# Copyright 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
from typing import Dict, List, Optional

import torch

import monai.deploy.core as md
from monai.apps.detection.networks.retinanet_detector import RetinaNetDetector
from monai.apps.detection.transforms.dictionary import (
    AffineBoxToImageCoordinated,
    AffineBoxToWorldCoordinated,
    ClipBoxToImaged,
)
from monai.apps.detection.utils.anchor_utils import AnchorGeneratorWithAnchorShape
from monai.deploy.core import AppContext, ConditionType, ExecutionContext, Fragment, Image, InputContext, IOType, Operator, OperatorSpec, OutputContext
from monai.deploy.core.domain import Domain
from monai.deploy.operators.monai_seg_inference_operator import InMemImageReader
from monai.deploy.utils.importutil import optional_import
from monai.transforms import (
    Compose,
    CopyItemsd,
    DeleteItemsd,
    EnsureChannelFirstd,
    EnsureTyped,
    LoadImaged,
    Orientationd,
    ScaleIntensityRanged,
    Spacingd,
    ToDeviced,
    ToTensord,
)

sliding_window_inference, _ = optional_import("monai.inferers", name="sliding_window_inference")


class DetectionResult(Domain):
    def __init__(self, box_data, label_data, score_data, metadata: Optional[Dict] = None):
        super().__init__(metadata)
        self._box_data = box_data
        self._label_data = label_data
        self._score_data = score_data

    @property
    def realworld_box_data(self):
        return self._box_data

    @property
    def box_data(self):
        return self._box_data

    @property
    def label_data(self):
        return self._label_data

    @property
    def score_data(self):
        return self._score_data


class DetectionResultList(Domain):
    def __init__(self, detection_list: List[DetectionResult], metadata: Optional[Dict] = None):
        super().__init__(metadata)
        self._detection_list = detection_list

    @property
    def detection_list(self):
        return self._detection_list


# @md.input("image", Image, IOType.IN_MEMORY)
# @md.output("detections", DetectionResultList, IOType.IN_MEMORY)
# @md.env(pip_packages=["monai==0.9.0", "torch>=1.10", "numpy>=1.21", "nibabel"])
class LungNoduleInferenceOperator(Operator):
    def __init__(
            self,
            fragment: Fragment,
            *args,
            app_context: AppContext,
            model_path: str = "model/model.ts",
            model_name: str = "",
            **kwargs,
            ):

        self.logger = logging.getLogger("{}.{}".format(__name__, type(self).__name__))
        self._input_name_image = "image"
        self._output_name_det = "detections"
        self._input_dataset_key = "image"
        self._input_dataset_orig_key = "image_orig"
        self._pred_box_regression = "box_regression"
        self._pred_label = "box_label"
        self._pred_score = "box_score"
        self._pred_labels = "labels"
        self._app_context = app_context
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._model_path = os.path.abspath(model_path)
        self._model_name = model_name
        self._model = None
        self._init_completed = False  # wati till the first call to complete init with model, etc.

        super().__init__(fragment, *args, **kwargs)

    def setup(self, spec: OperatorSpec):
        spec.input(self._input_name_image)
        spec.output(self._output_name_det)

    def _delayed_init(self, context: AppContext):
        if not self._init_completed:
            self.logger.info("Completing the model loading and detector creation.")
            if context.models:
                # `context.models.get(model_name)` returns a model instance if exists.
                # If model_name is not specified and only one model exists, it returns that model.
                self._model = context.models.get(self._model_name)

                # The model loaded in the context is missing expected network attrs when inference runs.
                # So need to get the path of the model in the context, then load it explicitly in this function.
                self._model_path = self._model.path

            self.logger.info(f"Loading TorchScript model from: {self._model_path}")
            self._model = torch.jit.load(self._model_path, map_location=self.device)

        self.logger.info("Loading model into RetinaNetDetector")
        self.returned_layers = [1, 2]
        self.base_achor_shapes = [[6, 8, 4], [8, 6, 5], [10, 10, 6]]
        anchor_generator = AnchorGeneratorWithAnchorShape(
            feature_map_scales=[1, 2, 4],
            base_anchor_shapes=self.base_achor_shapes,
        )
        self.detector = RetinaNetDetector(
            network=self._model,
            anchor_generator=anchor_generator,
        )
        self.detector.set_box_selector_parameters(
            score_thresh=0.1,
            topk_candidates_per_level=1000,
            nms_thresh=0.22,
            detections_per_img=100,
        )
        self.detector.set_sliding_window_inferer(
            roi_size=[240, 240, 160],
            overlap=0.5,
            sw_batch_size=1,
            mode="gaussian",
            device="cpu",
        )
        self.detector.eval()

        self._init_completed = True

    def compute(self, op_input: InputContext, op_output: OutputContext, context: ExecutionContext):

        input_image = op_input.receive(self._input_name_image)
        if not input_image:
            raise ValueError("Input image not found.")

        self._delayed_init(context=self._app_context)  # The ExecutionContext does not have all info

        image_reader = InMemImageReader(input_image)

        with torch.no_grad():
            processed_image = self.pre_process(image_reader)(
                (
                    {
                        self._input_dataset_key: input_image.metadata().get("SeriesInstanceUID", "Img_in_context"),
                    },
                    image_reader,
                )
            )

            inference_outputs = self.detector(processed_image[self._input_dataset_key], use_inferer=True)

            pred_boxes = []
            processed_image[self._input_dataset_key] = torch.squeeze(processed_image[self._input_dataset_key], dim=0)
            for inference_output in inference_outputs:
                processed_image[self._pred_box_regression] = inference_output[self.detector.target_box_key]
                processed_image[self._pred_labels] = inference_output[self.detector.target_label_key]
                processed_image[self._pred_score] = inference_output[self.detector.pred_score_key]

                processed_image = self.post_process()(processed_image)
                pred_boxes.append(
                    DetectionResult(
                        box_data=processed_image[self._pred_box_regression].numpy(),
                        label_data=processed_image[self._pred_labels].numpy(),
                        score_data=processed_image[self._pred_score].numpy(),
                    )
                )

            op_output.set(DetectionResultList(pred_boxes), "detections")

    def pre_process(self, img_reader) -> Compose:
        """Composes transforms for preprocessing input before predicting on a model."""

        image_key = self._input_dataset_key
        orig_image_key = self._input_dataset_orig_key
        return Compose(
            [
                LoadImaged(
                    keys=image_key,
                    reader=img_reader,
                    # affine_lps_to_ras=True,  # load raw, no need for this
                ),
                EnsureChannelFirstd(keys=image_key, channel_dim="no_channel"),
                Orientationd(
                    keys=image_key,
                    axcodes="RAS",
                ),
                Spacingd(keys=image_key, pixdim=(0.703125, 0.703125, 1.25)),
                # AddChanneld(keys=image_key),  # Function deprecated already
                EnsureChannelFirstd(keys=image_key, channel_dim="no_channel"),
                ScaleIntensityRanged(image_key, a_min=-1024.0, a_max=300.0, b_min=0.0, b_max=1.0, clip=True),
                EnsureTyped(image_key),

                # CopyItemsd(
                #     keys=[image_key, f"{image_key}_meta_dict"],
                #     names=[orig_image_key, f"{orig_image_key}_meta_dict"],
                # ),
                # ToTensord(keys=image_key),
                # ToDeviced(keys=image_key, device=self.device),
            ],
            unpack_items=True,
            map_items=False,
        )

    def post_process(self) -> Compose:
        """Composes transforms for postprocessing the prediction results."""

        return Compose(
            [
                ClipBoxToImaged(
                    box_keys=self._pred_box_regression,
                    box_ref_image_keys=self._input_dataset_key,
                    label_keys=[self._pred_labels, self._pred_score],
                    remove_empty=True,
                ),
                AffineBoxToWorldCoordinated(
                    box_keys=self._pred_box_regression,
                    box_ref_image_keys=self._input_dataset_key,
                    affine_lps_to_ras=True,
                ),
                # AffineBoxToImageCoordinated(
                #     box_keys=[self._pred_box_regression],
                #     box_ref_image_keys=self._input_dataset_orig_key,
                #     affine_lps_to_ras=True,
                # ),
                DeleteItemsd(key=self._input_dataset_key)
            ]
        )
