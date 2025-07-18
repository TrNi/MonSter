from __future__ import print_function, division
import sys
sys.path.append('core')
import os
import argparse
import time
import logging
import numpy as np
import torch
from tqdm import tqdm
from core.monster import Monster, autocast
from torch import nn
import core.stereo_datasets as datasets
from core.utils.utils import InputPadder
from PIL import Image
import cv2
import torch.nn.functional as F

def resize_image(img_chw, target_h, target_w, interpolation=cv2.INTER_LINEAR):
    # img_chw: C x H x W numpy array    
    img_hwc = np.transpose(img_chw, (1, 2, 0))
    resized_hwc = cv2.resize(img_hwc, (target_w, target_h), interpolation=interpolation)
    resized_chw = np.transpose(resized_hwc, (2, 0, 1))
    
    return resized_chw

def resize_batch(batch_nchw, target_h, target_w, interpolation=cv2.INTER_LINEAR):
    return np.stack([resize_image(img, target_h, target_w, interpolation) for img in batch_nchw])

def pad_to_even_multiple(x, n=32):
    """Pad tensor on the right/bottom so H and W are multiples of *n* and even."""
    _, _, h, w = x.shape
    h_pad = (-h) % n
    w_pad = (-w) % n
    # make them even to avoid odd-sized upsampling results
    if (h + h_pad) % 2:
        h_pad += n
    if (w + w_pad) % 2:
        w_pad += n
    return F.pad(x, (0, w_pad, 0, h_pad)), (h_pad, w_pad)

class NormalizeTensor(object):
    """Normalize a tensor by given mean and std."""
    
    def __init__(self, mean, std):
        self.mean = torch.tensor(mean)
        self.std = torch.tensor(std)
    
    def __call__(self, tensor):
        """
        Args:
            tensor (Tensor): Tensor image of size (C, H, W) to be normalized.
            
        Returns:
            Tensor: Normalized Tensor image.
        """
        # Ensure mean and std have the same number of channels as the input tensor
        Device = tensor.device
        self.mean = self.mean.to(Device)
        self.std = self.std.to(Device)

        # Normalize the tensor
        if self.mean.ndimension() == 1:
            self.mean = self.mean[:, None, None]
        if self.std.ndimension() == 1:
            self.std = self.std[:, None, None]

        return (tensor - self.mean) / self.std
    

def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

@torch.no_grad()
def validate_eth3d(model, iters=32, mixed_prec=False):
    """ Peform validation using the ETH3D (train) split """
    model.eval()
    aug_params = {}
    val_dataset = datasets.ETH3D(aug_params)

    out_list, epe_list = [], []
    for val_id in range(len(val_dataset)):
        (imageL_file, imageR_file, GT_file), image1, image2, flow_gt, valid_gt = val_dataset[val_id]
        image1 = image1[None].cuda()
        image2 = image2[None].cuda()

        padder = InputPadder(image1.shape, divis_by=32)
        image1, image2 = padder.pad(image1, image2)
        with torch.no_grad():
            with autocast("cuda", enabled=mixed_prec):
                flow_pr = model(image1, image2, iters=iters, test_mode=True)

        flow_pr = padder.unpad(flow_pr.float()).cpu().squeeze(0)
        assert flow_pr.shape == flow_gt.shape, (flow_pr.shape, flow_gt.shape)
        epe = torch.sum((flow_pr - flow_gt)**2, dim=0).sqrt()

        epe_flattened = epe.flatten()

        occ_mask = Image.open(GT_file.replace('disp0GT.pfm', 'mask0nocc.png'))

        occ_mask = np.ascontiguousarray(occ_mask).flatten()

        val = (valid_gt.flatten() >= 0.5) & (occ_mask == 255)
        # val = (valid_gt.flatten() >= 0.5)
        out = (epe_flattened > 1.0)
        image_out = out[val].float().mean().item()
        image_epe = epe_flattened[val].mean().item()
        logging.info(f"ETH3D {val_id+1} out of {len(val_dataset)}. EPE {round(image_epe,4)} D1 {round(image_out,4)}")
        epe_list.append(image_epe)
        out_list.append(image_out)

    epe_list = np.array(epe_list)
    out_list = np.array(out_list)

    epe = np.mean(epe_list)
    d1 = 100 * np.mean(out_list)

    print("Validation ETH3D: EPE %f, D1 %f" % (epe, d1))
    return {'eth3d-epe': epe, 'eth3d-d1': d1}


@torch.no_grad()
def validate_kitti(model, iters=32, mixed_prec=False):
    """ Peform validation using the KITTI-2015 (train) split """
    model.eval()
    # aug_params = {'crop_size': list([540, 960])}
    aug_params = {}
    val_dataset = datasets.KITTI(aug_params, image_set='training')
    torch.backends.cudnn.benchmark = True

    out_list, epe_list, elapsed_list = [], [], []
    for val_id in range(len(val_dataset)):
        (imageL_file, _, _), image1, image2, flow_gt, valid_gt = val_dataset[val_id]
        image1 = image1[None].cuda()
        image2 = image2[None].cuda()
    
        padder = InputPadder(image1.shape, divis_by=32)
        image1, image2 = padder.pad(image1, image2)

        with torch.no_grad():
            with autocast("cuda", enabled=mixed_prec):
                start = time.time()
                flow_pr = model(image1, image2, iters=iters, test_mode=True)
                end = time.time()

        if val_id > 50:
            elapsed_list.append(end-start)
        flow_pr = padder.unpad(flow_pr).cpu().squeeze(0)

        assert flow_pr.shape == flow_gt.shape, (flow_pr.shape, flow_gt.shape)
        epe = torch.sum((flow_pr - flow_gt)**2, dim=0).sqrt()

        epe_flattened = epe.flatten()
        val = (valid_gt.flatten() >= 0.5) & (flow_gt.abs().flatten() < 192)
        # val = valid_gt.flatten() >= 0.5

        out = (epe_flattened > 3.0)
        image_out = out[val].float().mean().item()
        image_epe = epe_flattened[val].mean().item()
        if val_id < 9 or (val_id+1)%10 == 0:
            logging.info(f"KITTI Iter {val_id+1} out of {len(val_dataset)}. EPE {round(image_epe,4)} D1 {round(image_out,4)}. Runtime: {format(end-start, '.3f')}s ({format(1/(end-start), '.2f')}-FPS)")
        epe_list.append(epe_flattened[val].mean().item())
        out_list.append(out[val].cpu().numpy())

        # if val_id > 20:
        #     break

    epe_list = np.array(epe_list)
    out_list = np.concatenate(out_list)

    epe = np.mean(epe_list)
    d1 = 100 * np.mean(out_list)

    avg_runtime = np.mean(elapsed_list)

    print(f"Validation KITTI: EPE {epe}, D1 {d1}, {format(1/avg_runtime, '.2f')}-FPS ({format(avg_runtime, '.3f')}s)")
    return {'kitti-epe': epe, 'kitti-d1': d1}


@torch.no_grad()
def validate_vkitti(model, iters=32, mixed_prec=False):
    """ Peform validation using the vkitti (train) split """
    model.eval()
    aug_params = {}
    val_dataset = datasets.VKITTI2(aug_params)
    torch.backends.cudnn.benchmark = True

    out_list, epe_list, elapsed_list = [], [], []
    for val_id in range(len(val_dataset)):
        _, image1, image2, flow_gt, valid_gt = val_dataset[val_id]
        image1 = image1[None].cuda()
        image2 = image2[None].cuda()

        padder = InputPadder(image1.shape, divis_by=32)
        image1, image2 = padder.pad(image1, image2)

        with autocast("cuda", enabled=mixed_prec):
            start = time.time()
            flow_pr = model(image1, image2, iters=iters, test_mode=True)
            end = time.time()

        if val_id > 50:
            elapsed_list.append(end - start)
        flow_pr = padder.unpad(flow_pr).cpu().squeeze(0)

        assert flow_pr.shape == flow_gt.shape, (flow_pr.shape, flow_gt.shape)
        epe = torch.sum((flow_pr - flow_gt) ** 2, dim=0).sqrt()

        epe_flattened = epe.flatten()
        val = (valid_gt.flatten() >= 0.5) & (flow_gt.abs().flatten() < 192)
        # val = valid_gt.flatten() >= 0.5

        out = (epe_flattened > 3.0)
        image_out = out[val].float().mean().item()
        image_epe = epe_flattened[val].mean().item()
        if val_id < 9 or (val_id + 1) % 10 == 0:
            logging.info(
                f"VKITTI Iter {val_id + 1} out of {len(val_dataset)}. EPE {round(image_epe, 4)} D1 {round(image_out, 4)}. Runtime: {format(end - start, '.3f')}s ({format(1 / (end - start), '.2f')}-FPS)")
        epe_list.append(epe_flattened[val].mean().item())
        out_list.append(out[val].cpu().numpy())

        # if val_id > 20:
        #     break

    epe_list = np.array(epe_list)
    out_list = np.concatenate(out_list)

    epe = np.mean(epe_list)
    d1 = 100 * np.mean(out_list)

    avg_runtime = np.mean(elapsed_list)

    print(f"Validation VKITTI: EPE {epe}, D1 {d1}, {format(1 / avg_runtime, '.2f')}-FPS ({format(avg_runtime, '.3f')}s)")
    return {'vkitti-epe': epe, 'vkitti-d1': d1}



@torch.no_grad()
def validate_sceneflow(model, iters=32, mixed_prec=False):
    """ Peform validation using the Scene Flow (TEST) split """
    model.eval()
    val_dataset = datasets.SceneFlowDatasets(dstype='frames_finalpass', things_test=True)
    torch.backends.cudnn.benchmark = True

    out_list, epe_list, elapsed_list = [], [], []
    for val_id in tqdm(range(len(val_dataset))):
        _, image1, image2, flow_gt, valid_gt = val_dataset[val_id]

        image1 = image1[None].cuda()
        image2 = image2[None].cuda()

        padder = InputPadder(image1.shape, divis_by=32)
        image1, image2 = padder.pad(image1, image2)

        with autocast("cuda", enabled=mixed_prec):
            start = time.time()
            flow_pr = model(image1, image2, iters=iters, test_mode=True)
            end = time.time()
        # print(torch.cuda.memory_summary(device=None, abbreviated=False))
        if val_id > 50:
            elapsed_list.append(end-start)

        flow_pr = padder.unpad(flow_pr).cpu().squeeze(0)
        assert flow_pr.shape == flow_gt.shape, (flow_pr.shape, flow_gt.shape)

        # epe = torch.sum((flow_pr - flow_gt)**2, dim=0).sqrt()
        epe = torch.abs(flow_pr - flow_gt)

        epe = epe.flatten()
        val = (valid_gt.flatten() >= 0.5) & (flow_gt.abs().flatten() < 192)

        if(np.isnan(epe[val].mean().item())):
            continue

        out = (epe > 3.0)
        image_out = out[val].float().mean().item()
        image_epe = epe[val].mean().item()
        if val_id < 9 or (val_id + 1) % 10 == 0:
            logging.info(
                f"Scene Flow Iter {val_id + 1} out of {len(val_dataset)}. EPE {round(image_epe, 4)} D1 {round(image_out, 4)}. Runtime: {format(end - start, '.3f')}s ({format(1 / (end - start), '.2f')}-FPS)")

        print('epe', epe[val].mean().item())
        epe_list.append(epe[val].mean().item())
        out_list.append(out[val].cpu().numpy())

    epe_list = np.array(epe_list)
    out_list = np.concatenate(out_list)

    epe = np.mean(epe_list)
    d1 = 100 * np.mean(out_list)

    avg_runtime = np.mean(elapsed_list)
    # f = open('test.txt', 'a')
    # f.write("Validation Scene Flow: %f, %f\n" % (epe, d1))

    print(f"Validation Scene Flow: EPE {epe}, D1 {d1}, {format(1/avg_runtime, '.2f')}-FPS ({format(avg_runtime, '.3f')}s)" )
    return {'scene-disp-epe': epe, 'scene-disp-d1': d1}

@torch.no_grad()
def validate_driving(model, iters=32, mixed_prec=False):
    """ Peform validation using the DrivingStereo (test) split """
    model.eval()
    aug_params = {}
    # val_dataset = datasets.DrivingStereo(aug_params, image_set='test')
    val_dataset = datasets.DrivingStereo(aug_params, image_set='cloudy')
    print(len(val_dataset))
    torch.backends.cudnn.benchmark = True

    out_list, epe_list, elapsed_list = [], [], []
    out1_list, out2_list = [], []
    for val_id in range(len(val_dataset)):
        _, image1, image2, flow_gt, valid_gt = val_dataset[val_id]
        image1 = image1[None].cuda()
        image2 = image2[None].cuda()

        padder = InputPadder(image1.shape, divis_by=32)
        image1, image2 = padder.pad(image1, image2)

        with torch.autocast(device_type='cuda', enabled=mixed_prec):
            start = time.time()
            flow_pr = model(image1, image2, iters=iters, test_mode=True)
            end = time.time()

        if val_id > 50:
            elapsed_list.append(end-start)
        flow_pr = padder.unpad(flow_pr).cpu().squeeze(0)

        assert flow_pr.shape == flow_gt.shape, (flow_pr.shape, flow_gt.shape)
        epe = torch.sum((flow_pr - flow_gt)**2, dim=0).sqrt()

        epe_flattened = epe.flatten()
        val = (valid_gt.flatten() >= 0.5) & (flow_gt.abs().flatten() < 192)
        # val = valid_gt.flatten() >= 0.5

        out = (epe_flattened > 3.0)
        out1 = (epe_flattened > 1.0)
        out2 = (epe_flattened > 2.0)
        image_out = out[val].float().mean().item()
        image_epe = epe_flattened[val].mean().item()
        if val_id < 9 or (val_id+1)%10 == 0:
            logging.info(f"Driving Iter {val_id+1} out of {len(val_dataset)}. EPE {round(image_epe,4)} D1 {round(image_out,4)}. Runtime: {format(end-start, '.3f')}s ({format(1/(end-start), '.2f')}-FPS)")
        epe_list.append(epe_flattened[val].mean().item())
        out_list.append(out[val].cpu().numpy())
        out1_list.append(out1[val].cpu().numpy())
        out2_list.append(out2[val].cpu().numpy())

    epe_list = np.array(epe_list)
    out_list = np.concatenate(out_list)
    out1_list = np.concatenate(out1_list)
    out2_list = np.concatenate(out2_list)

    epe = np.mean(epe_list)
    d1 = 100 * np.mean(out_list)
    bad_2 = 100 * np.mean(out2_list)
    bad_1 = 100 * np.mean(out1_list)
    avg_runtime = np.mean(elapsed_list)

    print(f"Validation DrivingStereo: EPE {epe}, bad1 {bad_1}, bad2 {bad_2}, bad3 {d1}, {format(1/avg_runtime, '.2f')}-FPS ({format(avg_runtime, '.3f')}s)")
    return {'driving-epe': epe, 'driving-d1': d1}


@torch.no_grad()
def validate_middlebury(model, iters=32, split='F', mixed_prec=False):
    """ Peform validation using the Middlebury-V3 dataset """
    model.eval()
    aug_params = {}
    val_dataset = datasets.Middlebury(aug_params, split=split)

    out_list, epe_list = [], []
    for val_id in range(len(val_dataset)):
        (imageL_file, _, _), image1, image2, flow_gt, valid_gt = val_dataset[val_id]
        image1 = image1[None].cuda()
        image2 = image2[None].cuda()

        padder = InputPadder(image1.shape, divis_by=32)
        image1, image2 = padder.pad(image1, image2)

        with autocast("cuda", enabled=mixed_prec):
            flow_pr = model(image1, image2, iters=iters, test_mode=True)
        flow_pr = padder.unpad(flow_pr).cpu().squeeze(0)
        a = input('input something')
        print(a)

        assert flow_pr.shape == flow_gt.shape, (flow_pr.shape, flow_gt.shape)
        epe = torch.sum((flow_pr - flow_gt)**2, dim=0).sqrt()

        epe_flattened = epe.flatten()

        occ_mask = Image.open(imageL_file.replace('im0.png', 'mask0nocc.png')).convert('L')
        occ_mask = np.ascontiguousarray(occ_mask, dtype=np.float32).flatten()

        val = (valid_gt.reshape(-1) >= 0.5) & (flow_gt[0].reshape(-1) < 192) & (occ_mask==255)
        out = (epe_flattened > 2.0)
        image_out = out[val].float().mean().item()
        image_epe = epe_flattened[val].mean().item()
        logging.info(f"Middlebury Iter {val_id+1} out of {len(val_dataset)}. EPE {round(image_epe,4)} D1 {round(image_out,4)}")
        epe_list.append(image_epe)
        out_list.append(image_out)

    epe_list = np.array(epe_list)
    out_list = np.array(out_list)

    epe = np.mean(epe_list)
    d1 = 100 * np.mean(out_list)

    print(f"Validation Middlebury{split}: EPE {epe}, D1 {d1}")
    return {f'middlebury{split}-epe': epe, f'middlebury{split}-d1': d1}


import h5py

@torch.no_grad()
def batched_stereo_inference(args, left_h5_file, right_h5_file, out_dir, stereo_params_npz_file, 
                           iters=32, mixed_prec=False, batch_size=4):
    """
    Load batched left/right stereo images from HDF5 files, perform inference, and save the output.
    
    Parameters:
        args: arguments for the model.
        left_h5_file (str): Path to HDF5 file with left images (dataset: left).
        right_h5_file (str): Path to HDF5 file with right images (dataset: right).
        output_h5_file (str): Path to save output HDF5 file.
        stereo_params_npz_file (str): Path to stereo parameters .npz file.
        iters (int): Number of update iterations for the model.
        mixed_prec (bool): Whether to use AMP.
        batch_size (int): Number of images to process in each batch.
    """
   
    
    
    stereo_params = np.load(stereo_params_npz_file, allow_pickle=True)
    P1 = stereo_params['P1']
    #P1[:2] *= args.scale
    f_left = P1[0,0]
    baseline = stereo_params['baseline']

    #out_dir = Path(out_dir)
    os.makedirs(out_dir, exist_ok=True)       
    
    if left_h5_file and right_h5_file:
        try:
            with h5py.File(left_h5_file, 'r') as f:
                left_all = f['data'][()]   # or np.array(f['left'])
            with h5py.File(right_h5_file, 'r') as f:
                right_all = f['data'][()]
        except Exception as e:            
            with h5py.File(left_h5_file, 'r') as f:
                left_all = f['left'][()]   # or np.array(f['left'])
            with h5py.File(right_h5_file, 'r') as f:
                right_all = f['right'][()]
      
        print(left_all.shape, right_all.shape)
    
    if left_all.ndim==3:
        left_all = left_all[None]
        right_all = right_all[None]
    
    N,C,H,W = left_all.shape
    if args.process_only:
        N_stop = args.process_only
    else:
        N_stop = N
    N_max = N_stop
    # aspect ratio for Canon EOS 6D is 3/2. 3648
    # image size of about 1586x2379 works with batch_size of 1, 
    # with resize_factor of 2.3 at 28s/image, up to ~25 images.
    small_dim = min(H,W)
    large_dim = max(H,W)

    resize_factor = 1#max(round(small_dim/1586,1), round(large_dim/2379,1))
    resize_factor = 1.5
    print(f"Found {N} images,  applying resize_factor {resize_factor} Saving files to {out_dir}.")
    args.max_disp = int(np.ceil(W/resize_factor/4/64/3)*64*3)
    #print("args.max_disp", args.max_disp)
    model = torch.nn.DataParallel(Monster(args), device_ids=[0])

    total_params = sum(p.numel() for p in model.parameters()) / 1e6
    print(f"Total number of parameters: {total_params:.2f}M")

    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad) / 1e6
    print(f"Total number of trainable parameters: {trainable_params:.2f}M")

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s')

    if args.restore_ckpt is not None:
        assert args.restore_ckpt.endswith(".pth")
        logging.info("Loading checkpoint...")
        logging.info(args.restore_ckpt)
        assert os.path.exists(args.restore_ckpt)
        checkpoint = torch.load(args.restore_ckpt, weights_only=True)
        ckpt = dict()
        if 'state_dict' in checkpoint.keys():
            checkpoint = checkpoint['state_dict']
        for key in checkpoint:
            # ckpt['module.' + key] = checkpoint[key]
            if key.startswith("module."):
                ckpt[key] = checkpoint[key]  # 保持原样
            else:
                ckpt["module." + key] = checkpoint[key]  # 添加 "module."

        model.load_state_dict(ckpt, strict=True) # , weights_only=True)

        logging.info(f"Done loading checkpoint")

    model.cuda()
    model.eval()

    print(f"The model has {format(count_parameters(model)/1e6, '.2f')}M learnable parameters.")

    disp_all = []
    depth_all = []    

    torch.backends.cuda.preferred_linalg_library(backend= "magma")    
    # Process in batches
    with torch.no_grad():
        for i in tqdm(range(0, N, batch_size), desc="Processing batches"):            
            img0 = left_all[i:i+batch_size]
            img1 = right_all[i:i+batch_size]

            if len(img0.shape)==3:
                img0 = img0[None,...]

            if len(img1.shape)==3:
                img1 = img1[None,...]

            # image size of about 1500x2300 works with batch_size of 1, 
            # with resize_factor of 1.5 at 28s/image, up to ~25 images.

            img0 = resize_batch(img0, round(H/resize_factor) ,round(W/resize_factor))
            img1 = resize_batch(img1, round(H/resize_factor), round(W/resize_factor))

            img0 = torch.as_tensor(img0).cuda().float()
            img1 = torch.as_tensor(img1).cuda().float()

            padder = InputPadder(img0.shape, divis_by=32)
            img0, img1 = padder.pad(img0, img1)            
            img0, pad_hw = pad_to_even_multiple(img0, 8)
            img1, _      = pad_to_even_multiple(img1, 8)

            print(img0.shape, img1.shape)

            # Load batch
            # left_batch = left_data[i:i+batch_size]
            # right_batch = right_data[i:i+batch_size]
            
            # # Convert to tensor and process
            # image1 = torch.from_numpy(left_batch).permute(0, 3, 1, 2).float().cuda()
            # image2 = torch.from_numpy(right_batch).permute(0, 3, 1, 2).float().cuda()
            
            # padder = InputPadder(image1.shape, divis_by=32)
            # image1, image2 = padder.pad(image1, image2)

            
            with autocast("cuda", enabled=mixed_prec):
                flow_pr = model(img0, img1, iters=iters, test_mode=True)
            
            # Handle different model outputs
            if isinstance(flow_pr, (list, tuple)):
                flow_pr = flow_pr[-1]  # Take the last output if model returns multiple
            
            flow_pr = padder.unpad(flow_pr).cpu().numpy()
            
            # Convert flow to disparity (assuming horizontal flow)
            disp = flow_pr[:, 0, :, :]  # Take x-component of flow as disparity
            
            # Calculate depth
            depth = f_left * baseline / (np.abs(disp) + 1e-6)
            
            # Save results
            disp_all.append(disp)
            depth_all.append(depth)
            # disp_[i:i+batch_size] = disp.astype('float16')
            # depth_dset[i:i+batch_size] = depth.astype('float16')
            if i+args.batch_size >= N_stop:
                N_max = i + img0.shape[0]
                break

    disp_all = np.concatenate(disp_all, axis=0).reshape(N_max,round(H/resize_factor),round(W/resize_factor)).astype(np.float16)
    depth_all = np.concatenate(depth_all, axis=0).reshape(N_max,round(H/resize_factor),round(W/resize_factor)).astype(np.float16)

    with h5py.File(f'{args.out_dir}/leftview_disp_depth.h5', 'w') as f:
      f.create_dataset('disp', data=disp_all, compression='gzip')
      f.create_dataset('depth', data=depth_all, compression='gzip')      
    print(f"Saved results to {args.out_dir}")
    return 

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch_size', default=5, type=int)    
    parser.add_argument('--left_h5_file', default="", type=str)
    parser.add_argument('--right_h5_file', default="", type=str)
    parser.add_argument('--stereo_params_npz_file', default = "", type = str)    
    parser.add_argument('--restore_ckpt', help="restore checkpoint", required=True)
    parser.add_argument('--out_dir', default=f'../output/', type=str, help='the directory to save results')
    parser.add_argument('--save_numpy', action='store_true', help='save output as numpy arrays', default=True)
    parser.add_argument('--dav2_path', type=str, default="/data2/cjd/mono_fusion/checkpoints/depth_anything_v2_vitl.pth")
    # parser.add_argument('--batch_size', type=int, default=4, help='Batch size for inference')    
    #parser.add_argument('--restore_ckpt', help="restore checkpoint", default="/data2/cjd/mono_fusion/checkpoints/sceneflow.pth")
    # parser.add_argument('--left_h5', type=str, help='Path to HDF5 file with left images (dataset: left)')
    # parser.add_argument('--right_h5', type=str, help='Path to HDF5 file with right images (dataset: right)')
    # parser.add_argument('--output_h5', type=str, help='Path to save output HDF5 file')
    # parser.add_argument('--stereo_params', type=str, help='Path to stereo parameters .npz file')
    parser.add_argument('--dataset', help="dataset for evaluation", default='h5_arrays', choices=["eth3d", "kitti", "sceneflow", "vkitti", "driving", "h5_arrays"] + [f"middlebury_{s}" for s in 'FHQ'])
    parser.add_argument('--mixed_precision', default=False, action='store_true', help='use mixed precision')
    parser.add_argument('--valid_iters', type=int, default=32, help='number of flow-field updates during forward pass')

    # Architecure choices
    parser.add_argument('--encoder', type=str, default='vitl', choices=['vits', 'vitb', 'vitl', 'vitg'])
    parser.add_argument('--hidden_dims', nargs='+', type=int, default=[128]*3, help="hidden state and context dimensions")
    parser.add_argument('--corr_implementation', choices=["reg", "alt", "reg_cuda", "alt_cuda"], default="reg", help="correlation volume implementation")
    parser.add_argument('--shared_backbone', action='store_true', help="use a single backbone for the context and feature encoders")
    parser.add_argument('--corr_levels', type=int, default=2, help="number of levels in the correlation pyramid")
    parser.add_argument('--corr_radius', type=int, default=4, help="width of the correlation pyramid")
    parser.add_argument('--n_downsample', type=int, default=2, help="resolution of the disparity field (1/2^K)")
    parser.add_argument('--slow_fast_gru', action='store_true', help="iterate the low-res GRUs more frequently")
    parser.add_argument('--n_gru_layers', type=int, default=3, help="number of hidden GRU levels")
    parser.add_argument('--max_disp', type=int, default=192, help="max disp of geometry encoding volume")
    parser.add_argument("--process_only",default=None,type=int)
    
    
    args = parser.parse_args()

    model = nn.Identity()
    use_mixed_precision = args.corr_implementation.endswith("_cuda")

    if args.dataset == 'eth3d':
        validate_eth3d(model, iters=args.valid_iters, mixed_prec=use_mixed_precision)

    elif args.dataset == 'kitti':
        validate_kitti(model, iters=args.valid_iters, mixed_prec=use_mixed_precision)

    elif args.dataset in [f"middlebury_{s}" for s in 'FHQ']:
        validate_middlebury(model, iters=args.valid_iters, split=args.dataset[-1], mixed_prec=use_mixed_precision)

    elif args.dataset == 'sceneflow':
        validate_sceneflow(model, iters=args.valid_iters, mixed_prec=use_mixed_precision)

    elif args.dataset == 'vkitti':
        validate_vkitti(model, iters=args.valid_iters, mixed_prec=use_mixed_precision)

    elif args.dataset == 'driving':
        validate_driving(model, iters=args.valid_iters, mixed_prec=use_mixed_precision)

    elif args.dataset == 'h5_arrays':
        batched_stereo_inference(
            args,
            left_h5_file=args.left_h5_file,
            right_h5_file=args.right_h5_file,
            out_dir=args.out_dir,
            stereo_params_npz_file=args.stereo_params_npz_file,
            iters=args.valid_iters,
            mixed_prec=use_mixed_precision,
            batch_size=args.batch_size
        )
