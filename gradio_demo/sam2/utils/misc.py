# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import os
import warnings
from threading import Thread
from typing import Dict, List, Union

import numpy as np
import torch
from PIL import Image
from torch.nn.attention import SDPBackend
from einops import rearrange
from tqdm import tqdm
import torch.nn.functional as F

VARIANTS: List[str] = ["tiny", "small", "base_plus", "large"]

variant_to_config_mapping: Dict[str, str] = {
    "tiny": "sam2_hiera_t.yaml",
    "small": "sam2_hiera_s.yaml",
    "base_plus": "sam2_hiera_b+.yaml",
    "large": "sam2_hiera_l.yaml",
}


def get_sdp_backends(dropout_p: float) -> Union[List[SDPBackend], SDPBackend]:
    backends = []
    if torch.cuda.is_available():
        use_flash_attn = torch.cuda.get_device_properties(0).major >= 8
        pytorch_version = tuple(int(v) for v in torch.__version__.split(".")[:2])

        if torch.cuda.get_device_properties(0).major < 7:
            backends.append(SDPBackend.EFFICIENT_ATTENTION)

        if use_flash_attn:
            backends.append(SDPBackend.FLASH_ATTENTION)

        if pytorch_version < (2, 2) or not use_flash_attn:
            backends.append(SDPBackend.MATH)

        if (
            SDPBackend.EFFICIENT_ATTENTION in backends and dropout_p > 0.0
        ) and SDPBackend.MATH not in backends:
            backends.append(SDPBackend.MATH)

    else:
        backends.extend([SDPBackend.EFFICIENT_ATTENTION, SDPBackend.MATH])

    return backends


def get_connected_components(mask):
    """
    Get the connected components (8-connectivity) of binary masks of shape (N, 1, H, W).

    Inputs:
    - mask: A binary mask tensor of shape (N, 1, H, W), where 1 is foreground and 0 is
            background.

    Outputs:
    - labels: A tensor of shape (N, 1, H, W) containing the connected component labels
              for foreground pixels and 0 for background pixels.
    - counts: A tensor of shape (N, 1, H, W) containing the area of the connected
              components for foreground pixels and 0 for background pixels.
    """
    from sam2 import _C

    return _C.get_connected_componnets(mask.to(torch.uint8).contiguous())


def mask_to_box(masks: torch.Tensor):
    """
    compute bounding box given an input mask

    Inputs:
    - masks: [B, 1, H, W] boxes, dtype=torch.Tensor

    Returns:
    - box_coords: [B, 1, 4], contains (x, y) coordinates of top left and bottom right box corners, dtype=torch.Tensor
    """
    B, _, h, w = masks.shape
    device = masks.device
    xs = torch.arange(w, device=device, dtype=torch.int32)
    ys = torch.arange(h, device=device, dtype=torch.int32)
    grid_xs, grid_ys = torch.meshgrid(xs, ys, indexing="xy")
    grid_xs = grid_xs[None, None, ...].expand(B, 1, h, w)
    grid_ys = grid_ys[None, None, ...].expand(B, 1, h, w)
    min_xs, _ = torch.min(torch.where(masks, grid_xs, w).flatten(-2), dim=-1)
    max_xs, _ = torch.max(torch.where(masks, grid_xs, -1).flatten(-2), dim=-1)
    min_ys, _ = torch.min(torch.where(masks, grid_ys, h).flatten(-2), dim=-1)
    max_ys, _ = torch.max(torch.where(masks, grid_ys, -1).flatten(-2), dim=-1)
    bbox_coords = torch.stack((min_xs, min_ys, max_xs, max_ys), dim=-1)

    return bbox_coords


def _load_img_as_tensor(img_path, image_size):
    img_pil = Image.open(img_path)
    img_np = np.array(img_pil.convert("RGB").resize((image_size, image_size)))
    if img_np.dtype == np.uint8:  # np.uint8 is expected for JPEG images
        img_np = img_np / 255.0
    else:
        raise RuntimeError(f"Unknown image dtype: {img_np.dtype} on {img_path}")
    img = torch.from_numpy(img_np).permute(2, 0, 1)
    video_width, video_height = img_pil.size  # the original video size
    return img, video_height, video_width


class AsyncVideoFrameLoader:
    """
    A list of video frames to be load asynchronously without blocking session start.
    """

    def __init__(self, img_paths, image_size, img_mean, img_std, device):
        self.img_paths = img_paths
        self.image_size = image_size
        self.img_mean = img_mean
        self.img_std = img_std
        self.device = device
        # items in `self._images` will be loaded asynchronously
        self.images = [None] * len(img_paths)
        # catch and raise any exceptions in the async loading thread
        self.exception = None
        # video_height and video_width be filled when loading the first image
        self.video_height = None
        self.video_width = None

        # load the first frame to fill video_height and video_width and also
        # to cache it (since it's most likely where the user will click)
        self.__getitem__(0)

        # load the rest of frames asynchronously without blocking the session start
        def _load_frames():
            try:
                for n in tqdm(range(len(self.images)), desc="frame loading (JPEG)"):
                    self.__getitem__(n)
            except Exception as e:
                self.exception = e

        self.thread = Thread(target=_load_frames, daemon=True)
        self.thread.start()

    def __getitem__(self, index):
        if self.exception is not None:
            raise RuntimeError("Failure in frame loading thread") from self.exception

        img = self.images[index]
        if img is not None:
            return img

        img, video_height, video_width = _load_img_as_tensor(
            self.img_paths[index], self.image_size
        )
        self.video_height = video_height
        self.video_width = video_width
        # normalize by mean and std
        img -= self.img_mean
        img /= self.img_std
        img = img.to(self.device)
        self.images[index] = img
        return img

    def __len__(self):
        return len(self.images)


def load_video_frames(
    video_path,
    image_size,
    images=None,
    img_mean=(0.485, 0.456, 0.406),
    img_std=(0.229, 0.224, 0.225),
    async_loading_frames=False,
    device="cpu",
):
    """
    Load the video frames from a directory of JPEG files ("<frame_index>.jpg" format).

    You can load a frame asynchronously by setting `async_loading_frames` to `True`.
    """
    img_mean = torch.tensor(img_mean, dtype=torch.float32)[:, None, None]
    img_std = torch.tensor(img_std, dtype=torch.float32)[:, None, None]

    if images is not None:
        images = torch.from_numpy(images).float()
        images = rearrange(images, "f h w c -> f c h w")
        images = F.interpolate(images, (image_size, image_size), mode="bilinear")
        video_height, video_width = images.shape[2:]
    else:
        if isinstance(video_path, str) and os.path.isdir(video_path):
            jpg_folder = video_path
        else:
            raise NotImplementedError("Only JPEG frames are supported at this moment")

        frame_names = [
            p
            for p in os.listdir(jpg_folder)
            if os.path.splitext(p)[-1] in [".jpg", ".jpeg", ".JPG", ".JPEG"]
        ]
        frame_names.sort(key=lambda p: int(os.path.splitext(p)[0]))
        num_frames = len(frame_names)
        if num_frames == 0:
            raise RuntimeError(f"no images found in {jpg_folder}")
        img_paths = [os.path.join(jpg_folder, frame_name) for frame_name in frame_names]

        images = torch.zeros(num_frames, 3, image_size, image_size, dtype=torch.float32)
        for n, img_path in enumerate(tqdm(img_paths, desc="frame loading (JPEG)")):
            images[n], video_height, video_width = _load_img_as_tensor(img_path, image_size)
    images = images.to(device)
    img_mean = img_mean.to(device)
    img_std = img_std.to(device)
    # normalize by mean and std
    images -= img_mean
    images /= img_std
    return images, video_height, video_width


def fill_holes_in_mask_scores(mask, max_area):
    """
    A post processor to fill small holes in mask scores with area under `max_area`.
    """
    # Holes are those connected components in background with area <= self.max_area
    # (background regions are those with mask scores <= 0)
    assert max_area > 0, "max_area must be positive"
    labels, areas = get_connected_components(mask <= 0)
    is_hole = (labels > 0) & (areas <= max_area)
    # We fill holes with a small positive mask score (0.1) to change them to foreground.
    mask = torch.where(is_hole, 0.1, mask)
    return mask


def concat_points(old_point_inputs, new_points, new_labels):
    """Add new points and labels to previous point inputs (add at the end)."""
    if old_point_inputs is None:
        points, labels = new_points, new_labels
    else:
        points = torch.cat([old_point_inputs["point_coords"], new_points], dim=1)
        labels = torch.cat([old_point_inputs["point_labels"], new_labels], dim=1)

    return {"point_coords": points, "point_labels": labels}
