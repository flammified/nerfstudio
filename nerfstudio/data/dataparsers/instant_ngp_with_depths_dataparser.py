# Copyright 2022 The Nerfstudio Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Data parser for instant ngp data with depthmaps attached"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Tuple, Type

import imageio
import numpy as np
import torch
from rich.console import Console

from nerfstudio.cameras import camera_utils
from nerfstudio.cameras.cameras import Cameras, CameraType
from nerfstudio.data.dataparsers.base_dataparser import (
    DataParser,
    DataParserConfig,
    DataparserOutputs,
)
from nerfstudio.data.scene_box import SceneBox
from nerfstudio.utils.io import load_from_json

CONSOLE = Console(width=120)

@dataclass
class InstantNGPWithDepthsDataParserConfig(DataParserConfig):
    """Instant-NGP dataset parser config"""

    _target: Type = field(default_factory=lambda: InstantNGPWithDepths)
    """target class to instantiate"""
    data: Path = Path("data/ours/posterv2")
    """Directory specifying location of data."""
    scale_factor: float = 1.0
    """How much to scale the camera origins by."""
    scene_scale: float = 0.33
    """How much to scale the scene."""


def get_depths(image_idx: int, depths):
    """function to process additional semantics and mask information

    Args:
        image_idx: specific image index to work with
        semantics: semantics data
    """
    return {"depth": depths[image_idx]}


@dataclass
class InstantNGPWithDepths(DataParser):
    """Instant NGP with depths Dataset"""

    config: InstantNGPDataParserConfig

    def _generate_dataparser_outputs(self, split="train"):

        meta = load_from_json(self.config.data / "transforms.json")
        image_filenames = []
        poses = []
        depths = []
        num_skipped_image_filenames = 0
        for frame in meta["frames"]:
            fname = self.config.data / Path(frame["file_path"])
            if not fname:
                num_skipped_image_filenames += 1
            else:
                image_filenames.append(fname)
                poses.append(np.array(frame["transform_matrix"]))
                depth = np.load(self.config.data / (frame["file_path"][:-4] + "_disp.npy"))
                depths.append(depth[0][0])
                img_0 = imageio.imread(fname)
                image_height, image_width = img_0.shape[:2]
        if num_skipped_image_filenames >= 0:
            CONSOLE.print(f"Skipping {num_skipped_image_filenames} files in dataset split {split}.")
        assert (
            len(image_filenames) != 0
        ), """
        No image files found. 
        You should check the file_paths in the transforms.json file to make sure they are correct.
        """
        poses = np.array(poses).astype(np.float32)
        poses[:, :3, 3] *= self.config.scene_scale

        camera_to_world = torch.from_numpy(poses[:, :3])  # camera to world transform

        distortion_params = camera_utils.get_distortion_params(
            k1=float(meta["k1"]), k2=float(meta["k2"]), p1=float(meta["p1"]), p2=float(meta["p2"])
        )

        # in x,y,z order
        # assumes that the scene is centered at the origin
        aabb_scale = meta["aabb_scale"]
        scene_box = SceneBox(
            aabb=torch.tensor(
                [[-aabb_scale, -aabb_scale, -aabb_scale], [aabb_scale, aabb_scale, aabb_scale]], dtype=torch.float32
            )
        )

        fl_x, fl_y = InstantNGPWithDepths.get_focal_lengths(meta)

        cameras = Cameras(
            fx=float(fl_x),
            fy=float(fl_y),
            cx=float(meta["cx"]),
            cy=float(meta["cy"]),
            distortion_params=distortion_params,
            height=int(meta["h"]),
            width=int(meta["w"]),
            camera_to_worlds=camera_to_world,
            camera_type=CameraType.PERSPECTIVE,
        )

        # TODO(ethan): add alpha background color
        dataparser_outputs = DataparserOutputs(
            image_filenames=image_filenames,
            cameras=cameras,
            scene_box=scene_box,
            additional_inputs={"semantics": {"func": get_depths, "kwargs": {"depths": depths}}},
            depths=depths,
        )

        return dataparser_outputs

    @classmethod
    def get_focal_lengths(cls, meta: Dict) -> Tuple[float, float]:
        """Reads or computes the focal length from transforms dict.
        Args:
            meta: metadata from transforms.json file.
        Returns:
            Focal lengths in the x and y directions. Error is raised if these cannot be calculated.
        """
        fl_x, fl_y = 0, 0

        def fov_to_focal_length(rad, res):
            return 0.5 * res / np.tanh(0.5 * rad)

        if "fl_x" in meta:
            fl_x = meta["fl_x"]
        elif "x_fov" in meta:
            fl_x = fov_to_focal_length(np.deg2rad(meta["x_fov"]), meta["w"])
        elif "camera_angle_x" in meta:
            fl_x = fov_to_focal_length(meta["camera_angle_x"], meta["w"])

        if "fl_y" in meta:
            fl_y = meta["fl_y"]
        elif "y_fov" in meta:
            fl_y = fov_to_focal_length(np.deg2rad(meta["y_fov"]), meta["h"])
        elif "camera_angle_y" in meta:
            fl_y = fov_to_focal_length(meta["camera_angle_y"], meta["h"])

        if fl_x == 0 or fl_y == 0:
            raise AttributeError("Focal length cannot be calculated from transforms.json (missing fields).")

        return (fl_x, fl_y)
