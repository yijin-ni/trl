# Copyright 2020-2025 The HuggingFace Team. All rights reserved.
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

import inspect
import os
import random
import textwrap
import warnings
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Union

import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from accelerate import PartialState
from accelerate.utils import tqdm
from datasets import Dataset, IterableDataset
from torch import autocast
from torch.utils.data import DataLoader
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BaseImageProcessor,
    DataCollator,
    FeatureExtractionMixin,
    PreTrainedModel,
    PreTrainedTokenizerBase,
    ProcessorMixin,
    Trainer,
    is_comet_available,
    is_wandb_available,
)
from transformers.data.data_collator import DataCollatorMixin
from transformers.models.auto.modeling_auto import MODEL_FOR_VISION_2_SEQ_MAPPING_NAMES
from transformers.trainer_callback import TrainerCallback
from transformers.trainer_utils import EvalLoopOutput
from transformers.utils import is_liger_kernel_available, is_peft_available

from ..data_utils_multi import maybe_apply_chat_template
from ..models import create_reference_model, prepare_deepspeed
from ..models.utils import prepare_fsdp
from .callbacks import SyncRefModelCallback
from .multidpo_config import MultiDPOConfig, FDivergenceConstants, FDivergenceType
from .utils import (
    RunningMoments,
    cap_exp,
    disable_dropout_in_model,
    empty_cache,
    flush_left,
    flush_right,
    generate_model_card,
    get_comet_experiment_url,
    log_table_to_comet_experiment,
    pad,
    pad_to_length,
    peft_module_casting_to_bf16,
    selective_log_softmax,
)


if is_peft_available():
    from peft import PeftConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training

if is_liger_kernel_available():
    from liger_kernel.chunked_loss import LigerFusedLinearDPOLoss


if is_wandb_available():
    import wandb


def shift_tokens_right(input_ids: torch.Tensor, decoder_start_token_id: int) -> torch.Tensor:
    """Shift input ids one token to the right, and pad with pad_token_id"""
    shifted_input_ids = input_ids.new_zeros(input_ids.shape)
    shifted_input_ids[:, 1:] = input_ids[:, :-1].clone()
    shifted_input_ids[:, 0] = decoder_start_token_id


@dataclass
class DataCollatorForPreference(DataCollatorMixin):
    """
    Data collator used for preference data. Inputs are dynamically padded to the maximum length of a batch if they are
    not all of the same length.

    Args:
        pad_token_id (`int`):
            Token ID to use for padding.
        return_tensors (`str`, *optional*, defaults to `"pt"`):
            Type of Tensor to return. Only `"pt"` is currently supported.

    Examples:
    ```python
    >>> from trl import DataCollatorForPreference

    >>> collator = DataCollatorForPreference(pad_token_id=0)
    >>> examples = [
    ...     {"prompt_input_ids": [1, 2, 3], "chosen_input_ids": [4, 5], "rejected_input_ids": [6]},
    ...     {"prompt_input_ids": [7, 8], "chosen_input_ids": [9, 10], "rejected_input_ids": [11, 12, 13]},
    ... ]
    >>> collator(examples)
    {'prompt_input_ids': tensor([[1, 2, 3],
                                 [0, 7, 8]]),
     'prompt_attention_mask': tensor([[1, 1, 1],
                                      [0, 1, 1]]),
     'chosen_input_ids': tensor([[ 4,  5],
                                 [ 9, 10]]),
     'chosen_attention_mask': tensor([[1, 1],
                                      [1, 1]]),
     'rejected_input_ids': tensor([[ 6,  0,  0],
                                   [11, 12, 13]]),
     'rejected_attention_mask': tensor([[1, 0, 0],
                                        [1, 1, 1]])
    }
    ```
    """

    pad_token_id: int
    return_tensors: str = "pt"

    def torch_call(self, examples: list[Union[list[int], Any, dict[str, Any]]]) -> dict[str, Any]:
        # Check if this is MultiDPO format (6-key) or standard DPO format (3-key)
        multidpo_keys = ["chosen_response_input_ids", "rejected_response_input_ids", "chosen_prompt_input_ids", "rejected_prompt_input_ids", "response_input_ids"]
        is_multidpo_format = all(key in examples[0] for key in multidpo_keys)
        
        # Convert to tensor - common fields
        prompt_input_ids = [torch.tensor(example["prompt_input_ids"]) for example in examples]
        prompt_attention_mask = [torch.ones_like(input_ids) for input_ids in prompt_input_ids]
        
        if is_multidpo_format:
            # MultiDPO 6-key format
            chosen_response_input_ids = [torch.tensor(example["chosen_response_input_ids"]) for example in examples]
            chosen_response_attention_mask = [torch.ones_like(input_ids) for input_ids in chosen_response_input_ids]
            rejected_response_input_ids = [torch.tensor(example["rejected_response_input_ids"]) for example in examples]
            rejected_response_attention_mask = [torch.ones_like(input_ids) for input_ids in rejected_response_input_ids]
            chosen_prompt_input_ids = [torch.tensor(example["chosen_prompt_input_ids"]) for example in examples]
            chosen_prompt_attention_mask = [torch.ones_like(input_ids) for input_ids in chosen_prompt_input_ids]
            rejected_prompt_input_ids = [torch.tensor(example["rejected_prompt_input_ids"]) for example in examples]
            rejected_prompt_attention_mask = [torch.ones_like(input_ids) for input_ids in rejected_prompt_input_ids]
            response_input_ids = [torch.tensor(example["response_input_ids"]) for example in examples]
            response_attention_mask = [torch.ones_like(input_ids) for input_ids in response_input_ids]
        else:
            # Standard DPO 3-key format (backward compatibility)
            chosen_input_ids = [torch.tensor(example["chosen_input_ids"]) for example in examples]
            chosen_attention_mask = [torch.ones_like(input_ids) for input_ids in chosen_input_ids]
            rejected_input_ids = [torch.tensor(example["rejected_input_ids"]) for example in examples]
            rejected_attention_mask = [torch.ones_like(input_ids) for input_ids in rejected_input_ids]
        
        # Vision support
        if "pixel_values" in examples[0]:
            pixel_values = [torch.tensor(example["pixel_values"]) for example in examples]
        if "pixel_attention_mask" in examples[0]:
            pixel_attention_mask = [torch.tensor(example["pixel_attention_mask"]) for example in examples]
            
        # Reference logps support (both formats)
        if "ref_chosen_logps" in examples[0] and "ref_rejected_logps" in examples[0]:
            ref_chosen_logps = torch.tensor([example["ref_chosen_logps"] for example in examples])
            ref_rejected_logps = torch.tensor([example["ref_rejected_logps"] for example in examples])
        
        # MultiDPO 4-part reference logps
        if "ref_chosen_logps_dpo" in examples[0]:
            ref_chosen_logps_dpo = torch.tensor([example["ref_chosen_logps_dpo"] for example in examples])
            ref_rejected_logps_dpo = torch.tensor([example["ref_rejected_logps_dpo"] for example in examples])
            ref_chosen_logps_adpo = torch.tensor([example["ref_chosen_logps_adpo"] for example in examples])
            ref_rejected_logps_adpo = torch.tensor([example["ref_rejected_logps_adpo"] for example in examples])

        # Pad and build output
        output = {}
        output["prompt_input_ids"] = pad(prompt_input_ids, padding_value=self.pad_token_id, padding_side="left")
        output["prompt_attention_mask"] = pad(prompt_attention_mask, padding_value=0, padding_side="left")
        
        if is_multidpo_format:
            # MultiDPO format output
            output["chosen_response_input_ids"] = pad(chosen_response_input_ids, padding_value=self.pad_token_id)
            output["chosen_response_attention_mask"] = pad(chosen_response_attention_mask, padding_value=0)
            output["rejected_response_input_ids"] = pad(rejected_response_input_ids, padding_value=self.pad_token_id)
            output["rejected_response_attention_mask"] = pad(rejected_response_attention_mask, padding_value=0)
            output["chosen_prompt_input_ids"] = pad(chosen_prompt_input_ids, padding_value=self.pad_token_id, padding_side="left")
            output["chosen_prompt_attention_mask"] = pad(chosen_prompt_attention_mask, padding_value=0, padding_side="left")
            output["rejected_prompt_input_ids"] = pad(rejected_prompt_input_ids, padding_value=self.pad_token_id, padding_side="left")
            output["rejected_prompt_attention_mask"] = pad(rejected_prompt_attention_mask, padding_value=0, padding_side="left")
            output["response_input_ids"] = pad(response_input_ids, padding_value=self.pad_token_id)
            output["response_attention_mask"] = pad(response_attention_mask, padding_value=0)
        else:
            # Standard DPO format output (backward compatibility)
            output["chosen_input_ids"] = pad(chosen_input_ids, padding_value=self.pad_token_id)
            output["chosen_attention_mask"] = pad(chosen_attention_mask, padding_value=0)
            output["rejected_input_ids"] = pad(rejected_input_ids, padding_value=self.pad_token_id)
            output["rejected_attention_mask"] = pad(rejected_attention_mask, padding_value=0)
        
        # Vision fields
        if "pixel_values" in examples[0]:
            output["pixel_values"] = pad(pixel_values, padding_value=0.0)
        if "pixel_attention_mask" in examples[0]:
            output["pixel_attention_mask"] = pad(pixel_attention_mask, padding_value=0)
        if "image_sizes" in examples[0]:
            output["image_sizes"] = torch.tensor([example["image_sizes"] for example in examples])
            
        # Reference logps
        if "ref_chosen_logps" in examples[0] and "ref_rejected_logps" in examples[0]:
            output["ref_chosen_logps"] = ref_chosen_logps
            output["ref_rejected_logps"] = ref_rejected_logps
            
        # MultiDPO 4-part reference logps
        if "ref_chosen_logps_dpo" in examples[0]:
            output["ref_chosen_logps_dpo"] = ref_chosen_logps_dpo
            output["ref_rejected_logps_dpo"] = ref_rejected_logps_dpo
            output["ref_chosen_logps_adpo"] = ref_chosen_logps_adpo
            output["ref_rejected_logps_adpo"] = ref_rejected_logps_adpo

        return output


class MultiDPOTrainer(Trainer):
    """
    Trainer for Multi-objective Direct Preference Optimization (MultiDPO) method.

    MultiDPO combines DPO and ADPO losses: λ * DPO_loss + (1-λ) * ADPO_loss
    - DPO loss: compares chosen vs rejected responses given the same prompt
    - ADPO loss: compares the same response given chosen vs rejected prompts

    This class is a wrapper around the [`transformers.Trainer`] class and inherits all of its attributes and methods.

    Args:
        model (`Union[str, PreTrainedModel]`):
            Model to be trained. Can be either:

            - A string, being the *model id* of a pretrained model hosted inside a model repo on huggingface.co, or a
              path to a *directory* containing model weights saved using
              [`~transformers.PreTrainedModel.save_pretrained`], e.g., `'./my_model_directory/'`. The model is loaded
              using [`~transformers.AutoModelForCausalLM.from_pretrained`] with the keyword arguments in
              `args.model_init_kwargs`.
            - A [`~transformers.PreTrainedModel`] object. Only causal language models are supported.
        ref_model (`PreTrainedModelWrapper`):
            Hugging Face transformer model with a casual language modelling head. Used for implicit reward computation
            and loss. If no reference model is provided, the trainer will create a reference model with the same
            architecture as the model to be optimized.
        args ([`MultiDPOConfig`], *optional*, defaults to `None`):
            Configuration for this trainer. If `None`, a default configuration is used.
        data_collator (`DataCollator`, *optional*):
            Function to use to form a batch from a list of elements of the processed `train_dataset` or `eval_dataset`.
            Will default to [`DataCollatorForPreference`].
        train_dataset ([`~datasets.Dataset`] or [`~datasets.IterableDataset`]):
            Dataset to use for training. MultiDPO supports two formats:
            
            - MultiDPO format (6-key): Each sample contains `prompt`, `chosen_response`, `rejected_response`, 
              `chosen_prompt`, `rejected_prompt`, and `response` keys.
            - Standard DPO format (3-key): Each sample contains `prompt`, `chosen`, and `rejected` keys 
              (backward compatibility).
        eval_dataset ([`~datasets.Dataset`], [`~datasets.IterableDataset`] or `dict[str, Union[Dataset, IterableDataset]]`):
            Dataset to use for evaluation. It must meet the same requirements as `train_dataset`.
        processing_class ([`~transformers.PreTrainedTokenizerBase`], *optional*, defaults to `None`):
            Processing class used to process the data. If `None`, the processing class is loaded from the model's name
            with [`~transformers.AutoTokenizer.from_pretrained`].
        compute_metrics (`Callable[[EvalPrediction], dict]`, *optional*):
            The function that will be used to compute metrics at evaluation. Must take a [`EvalPrediction`] and return
            a dictionary string to metric values. *Note* When passing TrainingArgs with `batch_eval_metrics` set to
            `True`, your compute_metrics function must take a boolean `compute_result` argument. This will be triggered
            after the last eval batch to signal that the function needs to calculate and return the global summary
            statistics rather than accumulating the batch-level statistics.
        callbacks (list of [`~transformers.TrainerCallback`], *optional*, defaults to `None`):
            List of callbacks to customize the training loop. Will add those to the list of default callbacks detailed
            in [here](https://huggingface.co/docs/transformers/main_classes/callback).

            If you want to remove one of the default callbacks used, use the [`~transformers.Trainer.remove_callback`]
            method.
        optimizers (`tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR]`, *optional*, defaults to `(None, None)`):
            A tuple containing the optimizer and the scheduler to use. Will default to an instance of [`AdamW`] on your
            model and a scheduler given by [`get_linear_schedule_with_warmup`] controlled by `args`.
        optimizer_cls_and_kwargs (`Tuple[Type[torch.optim.Optimizer], Dict[str, Any]]`, *optional*, defaults to `None`):
            A tuple containing the optimizer class and keyword arguments to use. Overrides `optim` and `optim_args` in
            `args`. Incompatible with the `optimizers` argument.
        preprocess_logits_for_metrics (`Callable[[torch.Tensor, torch.Tensor], torch.Tensor]`, *optional*, defaults to `None`):
            A function that preprocess the logits right before caching them at each evaluation step. Must take two
            tensors, the logits and the labels, and return the logits once processed as desired. The modifications made
            by this function will be reflected in the predictions received by `compute_metrics`.

            Note that the labels (second parameter) will be `None` if the dataset does not have them.
        peft_config ([`~peft.PeftConfig`], *optional*, defaults to `None`):
            PEFT configuration used to wrap the model. If `None`, the model is not wrapped.
    """

    _tag_names = ["trl", "dpo"]

    def __init__(
        self,
        model: Union[str, nn.Module, PreTrainedModel],
        ref_model: Optional[Union[PreTrainedModel, nn.Module, str]] = None,
        args: Optional[MultiDPOConfig] = None,
        data_collator: Optional[DataCollator] = None,  # type: ignore
        train_dataset: Optional[Union[Dataset, IterableDataset]] = None,
        eval_dataset: Optional[Union[Dataset, IterableDataset, dict[str, Union[Dataset, IterableDataset]]]] = None,
        processing_class: Optional[
            Union[PreTrainedTokenizerBase, BaseImageProcessor, FeatureExtractionMixin, ProcessorMixin]
        ] = None,
        compute_metrics: Optional[Callable[[EvalLoopOutput], dict]] = None,
        callbacks: Optional[list[TrainerCallback]] = None,
        optimizers: tuple[Optional[torch.optim.Optimizer], Optional[torch.optim.lr_scheduler.LambdaLR]] = (None, None),
        optimizer_cls_and_kwargs: Optional[tuple[type[torch.optim.Optimizer], dict[str, Any]]] = None,
        preprocess_logits_for_metrics: Optional[Callable[[torch.Tensor, torch.Tensor], torch.Tensor]] = None,
        peft_config: Optional["PeftConfig"] = None,
    ):
        # Args
        model_id = model if isinstance(model, str) else model.config._name_or_path
        if args is None:
            model_name = model_id.split("/")[-1]
            args = MultiDPOConfig(f"{model_name}-MultiDPO")

        # Handle the tokenizer
        if processing_class is None:
            processing_class = AutoTokenizer.from_pretrained(model_id)

        if args.padding_value is not None:
            self.padding_value = args.padding_value
        else:
            if hasattr(processing_class, "pad_token_id") and processing_class.pad_token_id is not None:
                self.padding_value = processing_class.pad_token_id
            elif hasattr(processing_class, "tokenizer") and processing_class.tokenizer.pad_token_id is not None:
                self.padding_value = processing_class.tokenizer.pad_token_id
            else:
                raise ValueError(
                    "`padding_value` is not specified in `MultiDPOConfig`, and `pad_token_id` is missing in the "
                    "`processing_class`. Please either set the `padding_value` argument in `MultiDPOConfig`, or set "
                    "`tokenizer.pad_token` (e.g., `tokenizer.pad_token = tokenizer.eos_token`) before instantiating "
                    "the trainer."
                )

        # Model
        if not isinstance(model, str) and ref_model is model:
            raise ValueError(
                "`model` and `ref_model` cannot be the same object. If you want `ref_model` to be the "
                "same as `model`, you must mass a copy of it, or `None` if you use peft."
            )

        if args.model_init_kwargs is not None and not isinstance(model, str):
            warnings.warn(
                "You passed model_init_kwargs to the `MultiDPOConfig`, but your model is already instantiated. "
                "The `model_init_kwargs` will be ignored."
            )
        if isinstance(model, str):
            model = self._create_model_from_path(model, args)

        if args.ref_model_init_kwargs is not None and not isinstance(ref_model, str):
            warnings.warn(
                "You passed ref_model_init_kwargs to the `MultiDPOConfig`, but your ref_model is already instantiated. "
                "The `ref_model_init_kwargs` will be ignored."
            )
        if isinstance(ref_model, str):
            ref_model = self._create_model_from_path(ref_model, args, is_ref=True)

        # PEFT configuration and model wrapping
        model = self._prepare_peft_model(model, ref_model, peft_config, args)

        if args.generate_during_eval and not (is_wandb_available() or is_comet_available()):
            raise ValueError(
                "`generate_during_eval=True` requires Weights and Biases or Comet to be installed."
                " Please install `wandb` or `comet-ml` to resolve."
            )

        self.is_encoder_decoder = model.config.is_encoder_decoder
        self.is_vision_model = model.config.model_type in MODEL_FOR_VISION_2_SEQ_MAPPING_NAMES.keys()
        self.is_peft_model = is_peft_available() and isinstance(model, PeftModel)
        self.model_adapter_name = args.model_adapter_name
        self.ref_adapter_name = args.ref_adapter_name
        self.reference_free = args.reference_free

        if ref_model:
            self.ref_model = ref_model
        elif self.is_peft_model or args.precompute_ref_log_probs:
            # The `model` with adapters turned off will be used as the reference model
            self.ref_model = None
        elif hasattr(self, 'is_deepspeed_enabled') and self.is_deepspeed_enabled:
            # For DeepSpeed ZeRO-3, create reference model directly with AutoModelForCausalLM
            model_name = getattr(model.config, '_name_or_path', None)
            if model_name:
                self.ref_model = AutoModelForCausalLM.from_pretrained(model_name)
            else:
                self.ref_model = None
        else:
            self.ref_model = create_reference_model(model)

        # Disable dropout in the model and reference model
        if args.disable_dropout:
            disable_dropout_in_model(model)
            if self.ref_model is not None:
                disable_dropout_in_model(self.ref_model)

        # Liger kernel
        if args.use_liger_loss:
            raise ValueError(
                "Liger loss is not currently supported with MultiDPO trainer. "
                "Please set `use_liger_loss=False` to use the MultiDPO trainer."
            )
            if not is_liger_kernel_available():
                raise ImportError(
                    "You set `use_liger_loss=True` but the liger kernel is not available. "
                    "Please install liger-kernel first: `pip install liger-kernel`"
                )
            if args.loss_type != "sigmoid":
                raise ValueError(
                    "You set `use_liger_loss=True` but the loss type is not `sigmoid`. "
                    "Please set `loss_type='sigmoid'` to use the liger kernel."
                )
            self.dpo_loss_fn = LigerFusedLinearDPOLoss(
                ignore_index=args.label_pad_token_id,
                beta=args.beta,
                use_ref_model=not args.reference_free,
                average_log_prob=False,
            )
        # The trainer estimates the number of FLOPs (floating-point operations) using the number of elements in the
        # input tensor associated with the key "input_ids". However, in DPO, the sampled data does not include the
        # "input_ids" key. Instead, the available keys are "prompt_input_ids", "chosen_input_ids", and
        # "rejected_input_ids". As a result, the trainer issues the warning: "Could not estimate the number of tokens
        # of the input, floating-point operations will not be computed." To suppress this warning, we set the
        # "estimate_tokens" key in the model's "warnings_issued" dictionary to True. This acts as a flag to indicate
        # that the warning has already been issued.
        model.warnings_issued["estimate_tokens"] = True

        # Data collator
        if data_collator is None:
            data_collator = DataCollatorForPreference(pad_token_id=self.padding_value)

        self.generate_during_eval = args.generate_during_eval
        self.label_pad_token_id = args.label_pad_token_id
        self.max_prompt_length = args.max_prompt_length
        self.max_completion_length = args.max_completion_length
        self.max_length = args.max_length
        self.truncation_mode = args.truncation_mode
        self.precompute_ref_log_probs = args.precompute_ref_log_probs
        self.use_logits_to_keep = args.use_logits_to_keep

        if args.padding_free:
            if model.config._attn_implementation != "flash_attention_2":
                warnings.warn(
                    "Padding-free training is enabled, but the attention implementation is not set to "
                    "'flash_attention_2'. Padding-free training flattens batches into a single sequence, and "
                    "'flash_attention_2' is the only known attention mechanism that reliably supports this. Using "
                    "other implementations may lead to unexpected behavior. To ensure compatibility, set "
                    "`attn_implementation='flash_attention_2'` in the model configuration, or verify that your "
                    "attention mechanism can handle flattened sequences."
                )
            if args.per_device_train_batch_size == 1:
                warnings.warn(
                    "You are using a per_device_train_batch_size of 1 with padding-free training. Using a batch size "
                    "of 1 anihilate the benefits of padding-free training. Please consider increasing the batch size "
                    "to at least 2."
                )
        self.padding_free = args.padding_free

        # Since ref_logs are precomputed on the first call to get_train/eval_dataloader
        # keep track of first called to avoid computation of future calls
        self._precomputed_train_ref_log_probs = False
        self._precomputed_eval_ref_log_probs = False

        if (
            args.loss_type in ["hinge", "ipo", "bco_pair", "sppo_hard", "nca_pair", "apo_zero", "apo_down"]
            and args.label_smoothing > 0
        ):
            warnings.warn(
                f"You are using the {args.loss_type} loss type that does not support label smoothing. The "
                "`label_smoothing` parameter will be ignored. Set `label_smoothing` to `0.0` to remove this warning.",
                UserWarning,
            )
        if args.loss_type == "kto_pair":
            raise ValueError("Support for kto_pair has been removed in DPOTrainer. Please use KTOTrainer.")

        self.beta = args.beta
        self.lambda_weight = args.lambda_weight
        self.label_smoothing = args.label_smoothing
        self.loss_type = args.loss_type
        self.aux_loss_enabled = getattr(model.config, "output_router_logits", False)
        self.use_weighting = args.use_weighting
        self.aux_loss_coef = getattr(model.config, "router_aux_loss_coef", 0.0)
        if self.aux_loss_enabled and self.aux_loss_coef == 0.0:
            warnings.warn(
                "You set `output_router_logits` to `True` in the model config, but `router_aux_loss_coef` is set to "
                "`0.0`, meaning the auxiliary loss will not be used. Either set `router_aux_loss_coef` to a value "
                "greater than `0.0`, or set `output_router_logits` to `False` if you don't want to use the auxiliary "
                "loss.",
                UserWarning,
            )

        self._stored_metrics = defaultdict(lambda: defaultdict(list))
        self.f_divergence_type = args.f_divergence_type
        self.f_divergence_params = {FDivergenceConstants.ALPHA_DIVERGENCE_COEF_KEY: args.f_alpha_divergence_coef}
        self.dataset_num_proc = args.dataset_num_proc

        # Dataset preparation
        train_dataset = self._prepare_dataset(train_dataset, processing_class, args, "train")
        if eval_dataset is not None:
            if isinstance(eval_dataset, dict):
                eval_dataset = {
                    key: self._prepare_dataset(dataset, processing_class, args, key)
                    for key, dataset in eval_dataset.items()
                }
            else:
                eval_dataset = self._prepare_dataset(eval_dataset, processing_class, args, "eval")

        super().__init__(
            model=model,
            args=args,
            data_collator=data_collator,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            processing_class=processing_class,
            compute_metrics=compute_metrics,
            callbacks=callbacks,
            optimizers=optimizers,
            optimizer_cls_and_kwargs=optimizer_cls_and_kwargs,
            preprocess_logits_for_metrics=preprocess_logits_for_metrics,
        )

        # Gradient accumulation requires scaled loss. Normally, loss scaling in the parent class depends on whether the
        # model accepts loss-related kwargs. Since we compute our own loss, this check is irrelevant. We set
        # self.model_accepts_loss_kwargs to False to enable scaling.
        self.model_accepts_loss_kwargs = False

        # Add tags for models that have been loaded with the correct transformers version
        if hasattr(self.model, "add_model_tags"):
            self.model.add_model_tags(self._tag_names)

        if not hasattr(self, "accelerator"):
            raise AttributeError(
                "Your `Trainer` does not have an `accelerator` object. Consider upgrading `transformers`."
            )

        # Deepspeed Zero-3 does not support precompute_ref_log_probs
        if self.is_deepspeed_enabled:
            if self.accelerator.state.deepspeed_plugin.zero_stage == 3 and self.precompute_ref_log_probs:
                raise ValueError(
                    "You cannot use `precompute_ref_log_probs=True` with Deepspeed ZeRO-3. Please set `precompute_ref_log_probs=False`."
                )

        if self.ref_model is None:
            if not (self.is_peft_model or self.precompute_ref_log_probs):
                raise ValueError(
                    "No reference model and model is not a Peft model. Try setting `precompute_ref_log_probs=True`"
                )
            if args.sync_ref_model:
                raise ValueError(
                    "You currently cannot use `ref_model=None` with TR-DPO method. Please provide `ref_model`."
                )
        else:
            if self.is_deepspeed_enabled:
                self.ref_model = prepare_deepspeed(self.ref_model, self.accelerator)
            elif self.is_fsdp_enabled:
                self.ref_model = prepare_fsdp(self.ref_model, self.accelerator)
            else:
                self.ref_model = self.accelerator.prepare_model(self.ref_model, evaluation_mode=True)

        if args.sync_ref_model:
            if self.precompute_ref_log_probs:
                raise ValueError(
                    "You cannot use `precompute_ref_log_probs=True` with TR-DPO method. Please set `precompute_ref_log_probs=False`."
                )

            self.add_callback(SyncRefModelCallback(ref_model=self.ref_model, accelerator=self.accelerator))

        if self.loss_type == "bco_pair":
            self.running = RunningMoments(self.accelerator)

    def _create_model_from_path(self, model_path: str, args: MultiDPOConfig, is_ref: bool = False) -> PreTrainedModel:
        """Creates a model from a path or model identifier."""
        if not is_ref:
            model_init_kwargs = args.model_init_kwargs or {}
        else:
            model_init_kwargs = args.ref_model_init_kwargs or {}

        # Handle torch dtype
        torch_dtype = model_init_kwargs.get("torch_dtype")
        if isinstance(torch_dtype, torch.dtype) or torch_dtype == "auto" or torch_dtype is None:
            pass  # torch_dtype is already a torch.dtype or "auto" or None
        elif isinstance(torch_dtype, str):  # it's a str, but not "auto"
            torch_dtype = getattr(torch, torch_dtype)
            model_init_kwargs["torch_dtype"] = torch_dtype
        else:
            raise ValueError(
                "Invalid `torch_dtype` passed to `MultiDPOConfig`. Expected either 'auto' or a string representing "
                f"a `torch.dtype` (e.g., 'float32'), but got {torch_dtype}."
            )
        # Disable caching if gradient checkpointing is enabled (not supported)
        # if args.gradient_checkpointing:
        #     model_init_kwargs["use_cache"] = False

        # Create model
        model = AutoModelForCausalLM.from_pretrained(model_path, **model_init_kwargs)
        return model

    def _prepare_peft_model(
        self, model: PreTrainedModel, ref_model: PreTrainedModel, peft_config: Any, args: MultiDPOConfig
    ) -> PreTrainedModel:
        """Prepares a model for PEFT training."""
        # Initialize this variable to False. This helps tracking the case when `peft_module_casting_to_bf16`
        # has been called in order to properly call autocast if needed.
        self._peft_has_been_casted_to_bf16 = False

        if not is_peft_available() and peft_config is not None:
            raise ValueError(
                "PEFT is not installed and you passed a `peft_config` in the trainer's kwargs, please install it to use the PEFT models"
            )
        elif is_peft_available() and peft_config is not None:
            # if model is a peft model and we have a peft_config, we merge and unload it first
            if isinstance(model, PeftModel):
                model = model.merge_and_unload()

            if ref_model is not None and not args.force_use_ref_model:
                raise ValueError(
                    "You passed both a ref_model and a peft_config. For training PEFT adapters with DPO there is no need to pass a reference"
                    " model. Please pass `ref_model=None` in case you want to train PEFT adapters, or pass a ref_model with `force_use_ref_model=True` in DPOTrainer's init."
                    " if you want to use a different ref_model."
                )

            if getattr(model, "is_loaded_in_8bit", False) or getattr(model, "is_loaded_in_4bit", False):
                _support_gc_kwargs = hasattr(
                    args, "gradient_checkpointing_kwargs"
                ) and "gradient_checkpointing_kwargs" in list(
                    inspect.signature(prepare_model_for_kbit_training).parameters
                )

                prepare_model_kwargs = {"use_gradient_checkpointing": args.gradient_checkpointing}

                if _support_gc_kwargs:
                    prepare_model_kwargs["gradient_checkpointing_kwargs"] = args.gradient_checkpointing_kwargs

                model = prepare_model_for_kbit_training(model, **prepare_model_kwargs)

            else:
                model = self._prepare_gradient_checkpointing(model, args)

            # get peft model with the given config
            model = get_peft_model(model, peft_config)
            if args.bf16 and getattr(model, "is_loaded_in_4bit", False):
                peft_module_casting_to_bf16(model)
                # If args.bf16 we need to explicitly call `generate` with torch amp autocast context manager
                self._peft_has_been_casted_to_bf16 = True

        else:
            model = self._prepare_gradient_checkpointing(model, args)

        return model

    def _prepare_gradient_checkpointing(self, model: PreTrainedModel, args: MultiDPOConfig):
        """Prepare the gradienting checkpointing for the model."""
        # For models that use gradient_checkpointing, we need to attach a hook that enables input
        # to explicitly have `requires_grad=True`, otherwise training will either silently
        # fail or completely fail.
        if args.gradient_checkpointing:
            # For backward compatibility with older versions of transformers
            if hasattr(model, "enable_input_require_grads"):
                model.enable_input_require_grads()
            else:

                def make_inputs_require_grad(module, input, output):
                    output.requires_grad_(True)

                model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)

        return model

    def _prepare_dataset(
        self,
        dataset: Union[Dataset, IterableDataset],
        processing_class: Union[PreTrainedTokenizerBase, BaseImageProcessor, FeatureExtractionMixin, ProcessorMixin],
        args: MultiDPOConfig,
        dataset_name: str,
    ) -> Union[Dataset, IterableDataset]:
        # Build the kwargs for the `map` function
        map_kwargs = {}
        if isinstance(dataset, Dataset):  # IterableDataset does not support num_proc nor writer_batch_size
            map_kwargs["num_proc"] = args.dataset_num_proc
            map_kwargs["writer_batch_size"] = 10

        with PartialState().main_process_first():
            # Skip the prompt extraction step. Assume the prompts and responses have been extracted from the beginning.
            # if isinstance(dataset, Dataset):  # `IterableDataset.map` does not support `desc`
            #     map_kwargs["desc"] = f"Extracting prompt in {dataset_name} dataset"
            # dataset = dataset.map(maybe_extract_prompt, **map_kwargs)

            # Apply the chat template if needed
            if isinstance(dataset, Dataset):  # `IterableDataset.map` does not support `desc`
                map_kwargs["desc"] = f"Applying chat template to {dataset_name} dataset"
            dataset = dataset.map(
                maybe_apply_chat_template, fn_kwargs={"tokenizer": processing_class, "tools": args.tools}, **map_kwargs
            )

            # Tokenize the dataset
            if isinstance(dataset, Dataset):  # `IterableDataset.map` does not support `desc`
                map_kwargs["desc"] = f"Tokenizing {dataset_name} dataset"

            # Determine which columns to remove based on dataset format
            sample_item = dataset[0] if hasattr(dataset, '__getitem__') else next(iter(dataset.take(1)))
            multidpo_keys = ["chosen_response", "rejected_response", "chosen_prompt", "rejected_prompt", "response"]
            standard_dpo_keys = ["chosen", "rejected"]
            
            is_multidpo_format = all(key in sample_item for key in multidpo_keys)
            
            if is_multidpo_format:
                # Remove MultiDPO format columns after tokenization
                columns_to_remove = multidpo_keys + ["prompt"]  # Keep tokenized versions
            else:
                # Remove standard DPO format columns after tokenization
                columns_to_remove = standard_dpo_keys  # Keep prompt for backward compatibility
            
            dataset = dataset.map(
                self.tokenize_row if not self.is_vision_model else self.process_row,
                remove_columns=columns_to_remove,
                fn_kwargs={
                    "processing_class": processing_class,
                    "max_prompt_length": args.max_prompt_length,
                    "max_completion_length": args.max_completion_length,
                    # for enc-dec, we add the special tokens ([bos_token] + prompt + [eos_token]; completion + [eos_token])
                    "add_special_tokens": False,
                },
                **map_kwargs,
            )

        return dataset

    @staticmethod
    def tokenize_row(features, processing_class, max_prompt_length, max_completion_length, add_special_tokens):
        """
        Tokenize a row of the dataset for MultiDPO training.

        Args:
            features (`dict[str, str]`):
                Row of the dataset, should contain the MultiDPO keys:
                - `"prompt"`: Original prompt/question
                - `"chosen_response"`: Preferred response to the prompt  
                - `"rejected_response"`: Non-preferred response to the prompt
                - `"chosen_prompt"`: Preferred version of the prompt/question
                - `"rejected_prompt"`: Non-preferred version of the prompt/question
                - `"response"`: Response that works with both chosen and rejected prompts
                
                For backward compatibility, also supports standard DPO format:
                - `"prompt"`, `"chosen"`, `"rejected"`
            processing_class (`PreTrainedTokenizerBase`):
                Processing class used to process the data.
            max_prompt_length (`int` or `None`):
                Maximum length of the prompt sequence. If `None`, the prompt sequence is not truncated.
            max_completion_length (`int` or `None`):
                Maximum length of the completion sequences. If `None`, the completion sequences are not truncated.
            add_special_tokens (`bool`):
                Whether to add special tokens to the sequences. Typically used for encoder-decoder models. If `True`,
                the prompt sequence will have a bos token prepended and an eos token appended. In any case, the
                completion sequences will have an eos token appended.

        Returns:
            `dict[str, list[int]]`:
                For MultiDPO format: Tokenized sequences with the keys `"prompt_input_ids"`, `"chosen_response_input_ids"`, 
                `"rejected_response_input_ids"`, `"chosen_prompt_input_ids"`, `"rejected_prompt_input_ids"`, and `"response_input_ids"`.
                
                For backward compatibility: `"prompt_input_ids"`, `"chosen_input_ids"`, and `"rejected_input_ids"`.

        Example:
        ```python
        >>> from transformers import GPT2Tokenizer

        >>> tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
        >>> # MultiDPO format
        >>> features = {
        ...     "prompt": "What is the capital?", 
        ...     "chosen_response": "Paris", 
        ...     "rejected_response": "London",
        ...     "chosen_prompt": "What is the capital of France?", 
        ...     "rejected_prompt": "What is the capital of England?", 
        ...     "response": "Paris"
        ... }
        >>> MultiDPOTrainer.tokenize_row(
        ...     features, tokenizer, max_prompt_length=10, max_completion_length=5, add_special_tokens=False
        ... )
        # Returns 6-key MultiDPO format
        ```
        """
        tokenizer = processing_class  # the processing class is a tokenizer
        
        # Check if this is MultiDPO format (6 keys) or standard DPO format (3 keys)
        multidpo_keys = ["prompt", "chosen_response", "rejected_response", "chosen_prompt", "rejected_prompt", "response"]
        standard_dpo_keys = ["prompt", "chosen", "rejected"]
        
        is_multidpo_format = all(key in features for key in multidpo_keys)
        is_standard_dpo_format = all(key in features for key in standard_dpo_keys)
        
        if is_multidpo_format:
            # MultiDPO 6-key format
            prompt_input_ids = tokenizer(features["prompt"], add_special_tokens=False)["input_ids"]
            chosen_response_input_ids = tokenizer(features["chosen_response"], add_special_tokens=False)["input_ids"]
            rejected_response_input_ids = tokenizer(features["rejected_response"], add_special_tokens=False)["input_ids"]
            chosen_prompt_input_ids = tokenizer(features["chosen_prompt"], add_special_tokens=False)["input_ids"]
            rejected_prompt_input_ids = tokenizer(features["rejected_prompt"], add_special_tokens=False)["input_ids"]
            response_input_ids = tokenizer(features["response"], add_special_tokens=False)["input_ids"]

            # Add special tokens (typically for encoder-decoder models)
            if add_special_tokens:
                if tokenizer.bos_token_id is not None:
                    prompt_input_ids = [tokenizer.bos_token_id] + prompt_input_ids
                    chosen_prompt_input_ids = [tokenizer.bos_token_id] + chosen_prompt_input_ids
                    rejected_prompt_input_ids = [tokenizer.bos_token_id] + rejected_prompt_input_ids
                if tokenizer.eos_token_id is not None:
                    prompt_input_ids = prompt_input_ids + [tokenizer.eos_token_id]
                    chosen_prompt_input_ids = chosen_prompt_input_ids + [tokenizer.eos_token_id]
                    rejected_prompt_input_ids = rejected_prompt_input_ids + [tokenizer.eos_token_id]
            
            # Add EOS tokens to all responses
            chosen_response_input_ids = chosen_response_input_ids + [tokenizer.eos_token_id]
            rejected_response_input_ids = rejected_response_input_ids + [tokenizer.eos_token_id]
            response_input_ids = response_input_ids + [tokenizer.eos_token_id]

            # Truncate prompt sequences
            if max_prompt_length is not None:
                prompt_input_ids = prompt_input_ids[-max_prompt_length:]
                chosen_prompt_input_ids = chosen_prompt_input_ids[-max_prompt_length:]
                rejected_prompt_input_ids = rejected_prompt_input_ids[-max_prompt_length:]
            
            # Truncate completion sequences
            if max_completion_length is not None:
                chosen_response_input_ids = chosen_response_input_ids[:max_completion_length]
                rejected_response_input_ids = rejected_response_input_ids[:max_completion_length]
                response_input_ids = response_input_ids[:max_completion_length]

            return {
                "prompt_input_ids": prompt_input_ids,
                "chosen_response_input_ids": chosen_response_input_ids,
                "rejected_response_input_ids": rejected_response_input_ids,
                "chosen_prompt_input_ids": chosen_prompt_input_ids,
                "rejected_prompt_input_ids": rejected_prompt_input_ids,
                "response_input_ids": response_input_ids,
            }
            
        elif is_standard_dpo_format:
            # Standard DPO 3-key format (backward compatibility)
            prompt_input_ids = tokenizer(features["prompt"], add_special_tokens=False)["input_ids"]
            chosen_input_ids = tokenizer(features["chosen"], add_special_tokens=False)["input_ids"]
            rejected_input_ids = tokenizer(features["rejected"], add_special_tokens=False)["input_ids"]

            # Add special tokens (typically for encoder-decoder models)
            if add_special_tokens:
                if tokenizer.bos_token_id is not None:
                    prompt_input_ids = [tokenizer.bos_token_id] + prompt_input_ids
                if tokenizer.eos_token_id is not None:
                    prompt_input_ids = prompt_input_ids + [tokenizer.eos_token_id]
            chosen_input_ids = chosen_input_ids + [tokenizer.eos_token_id]
            rejected_input_ids = rejected_input_ids + [tokenizer.eos_token_id]

            # Truncate prompt and completion sequences
            if max_prompt_length is not None:
                prompt_input_ids = prompt_input_ids[-max_prompt_length:]
            if max_completion_length is not None:
                chosen_input_ids = chosen_input_ids[:max_completion_length]
                rejected_input_ids = rejected_input_ids[:max_completion_length]

            return {
                "prompt_input_ids": prompt_input_ids,
                "chosen_input_ids": chosen_input_ids,
                "rejected_input_ids": rejected_input_ids,
            }
        else:
            # Invalid format
            available_keys = list(features.keys())
            raise ValueError(
                f"Invalid dataset format. Expected either:\n"
                f"- MultiDPO format with keys: {multidpo_keys}\n"
                f"- Standard DPO format with keys: {standard_dpo_keys}\n"
                f"Got keys: {available_keys}"
            )

    @staticmethod
    def process_row(features, processing_class, max_prompt_length, max_completion_length, add_special_tokens):
        """
        Same as `tokenize_row` but for vision models with MultiDPO support. Please refer to `tokenize_row` for more information.
        """
        processor, tokenizer = processing_class, processing_class.tokenizer  # the processing class is a processor
        
        # Check if this is MultiDPO format (6 keys) or standard DPO format (3 keys)
        multidpo_keys = ["prompt", "chosen_response", "rejected_response", "chosen_prompt", "rejected_prompt", "response"]
        standard_dpo_keys = ["prompt", "chosen", "rejected"]
        
        is_multidpo_format = all(key in features for key in multidpo_keys)
        is_standard_dpo_format = all(key in features for key in standard_dpo_keys)
        
        if is_multidpo_format:
            # MultiDPO format for vision models
            # Process main prompt with images
            processed_features = processor(images=features["images"], text=features["prompt"], add_special_tokens=False)
            prompt_input_ids = processed_features["input_ids"][0]
            pixel_values = processed_features["pixel_values"][0]
            
            # Process chosen and rejected prompts (may have different images)
            if "chosen_images" in features:
                chosen_processed = processor(images=features["chosen_images"], text=features["chosen_prompt"], add_special_tokens=False)
                chosen_prompt_input_ids = chosen_processed["input_ids"][0]
                chosen_pixel_values = chosen_processed["pixel_values"][0]
            else:
                # Use same images with different prompt text
                chosen_processed = processor(images=features["images"], text=features["chosen_prompt"], add_special_tokens=False)
                chosen_prompt_input_ids = chosen_processed["input_ids"][0] 
                chosen_pixel_values = pixel_values  # Reuse same images
                
            if "rejected_images" in features:
                rejected_processed = processor(images=features["rejected_images"], text=features["rejected_prompt"], add_special_tokens=False)
                rejected_prompt_input_ids = rejected_processed["input_ids"][0]
                rejected_pixel_values = rejected_processed["pixel_values"][0]
            else:
                # Use same images with different prompt text
                rejected_processed = processor(images=features["images"], text=features["rejected_prompt"], add_special_tokens=False)
                rejected_prompt_input_ids = rejected_processed["input_ids"][0]
                rejected_pixel_values = pixel_values  # Reuse same images
            
            # Tokenize responses
            chosen_response_input_ids = tokenizer(features["chosen_response"], add_special_tokens=False)["input_ids"]
            rejected_response_input_ids = tokenizer(features["rejected_response"], add_special_tokens=False)["input_ids"]
            response_input_ids = tokenizer(features["response"], add_special_tokens=False)["input_ids"]

            # Add special tokens (typically for encoder-decoder models)
            if add_special_tokens:
                if tokenizer.bos_token_id is not None:
                    prompt_input_ids = [tokenizer.bos_token_id] + prompt_input_ids
                    chosen_prompt_input_ids = [tokenizer.bos_token_id] + chosen_prompt_input_ids
                    rejected_prompt_input_ids = [tokenizer.bos_token_id] + rejected_prompt_input_ids
                if tokenizer.eos_token_id is not None:
                    prompt_input_ids = prompt_input_ids + [tokenizer.eos_token_id]
                    chosen_prompt_input_ids = chosen_prompt_input_ids + [tokenizer.eos_token_id]
                    rejected_prompt_input_ids = rejected_prompt_input_ids + [tokenizer.eos_token_id]
            
            # Add EOS tokens to all responses
            chosen_response_input_ids = chosen_response_input_ids + [tokenizer.eos_token_id]
            rejected_response_input_ids = rejected_response_input_ids + [tokenizer.eos_token_id]
            response_input_ids = response_input_ids + [tokenizer.eos_token_id]

            # Truncate prompt sequences
            if max_prompt_length is not None:
                prompt_input_ids = prompt_input_ids[-max_prompt_length:]
                chosen_prompt_input_ids = chosen_prompt_input_ids[-max_prompt_length:]
                rejected_prompt_input_ids = rejected_prompt_input_ids[-max_prompt_length:]
            
            # Truncate completion sequences
            if max_completion_length is not None:
                chosen_response_input_ids = chosen_response_input_ids[:max_completion_length]
                rejected_response_input_ids = rejected_response_input_ids[:max_completion_length]
                response_input_ids = response_input_ids[:max_completion_length]

            output = {
                "prompt_input_ids": prompt_input_ids,
                "pixel_values": pixel_values,
                "chosen_response_input_ids": chosen_response_input_ids,
                "rejected_response_input_ids": rejected_response_input_ids,
                "chosen_prompt_input_ids": chosen_prompt_input_ids,
                "rejected_prompt_input_ids": rejected_prompt_input_ids,
                "response_input_ids": response_input_ids,
            }

            # Add additional vision features if available
            if "pixel_attention_mask" in processed_features:
                output["pixel_attention_mask"] = processed_features["pixel_attention_mask"][0]
            if "image_sizes" in processed_features:
                output["image_sizes"] = processed_features["image_sizes"][0]

            return output
            
        elif is_standard_dpo_format:
            # Standard DPO format for vision models (backward compatibility)
            processed_features = processor(images=features["images"], text=features["prompt"], add_special_tokens=False)

            prompt_input_ids = processed_features["input_ids"][0]
            pixel_values = processed_features["pixel_values"][0]
            chosen_input_ids = tokenizer(features["chosen"], add_special_tokens=False)["input_ids"]
            rejected_input_ids = tokenizer(features["rejected"], add_special_tokens=False)["input_ids"]

            # Add special tokens (typically for encoder-decoder models)
            if add_special_tokens:
                if tokenizer.bos_token_id is not None:
                    prompt_input_ids = [tokenizer.bos_token_id] + prompt_input_ids
                if tokenizer.eos_token_id is not None:
                    prompt_input_ids = prompt_input_ids + [tokenizer.eos_token_id]
            chosen_input_ids = chosen_input_ids + [tokenizer.eos_token_id]
            rejected_input_ids = rejected_input_ids + [tokenizer.eos_token_id]

            # Truncate prompt and completion sequences
            if max_prompt_length is not None:
                prompt_input_ids = prompt_input_ids[-max_prompt_length:]
            if max_completion_length is not None:
                chosen_input_ids = chosen_input_ids[:max_completion_length]
                rejected_input_ids = rejected_input_ids[:max_completion_length]

            output = {
                "prompt_input_ids": prompt_input_ids,
                "pixel_values": pixel_values,
                "chosen_input_ids": chosen_input_ids,
                "rejected_input_ids": rejected_input_ids,
            }

            if "pixel_attention_mask" in processed_features:
                output["pixel_attention_mask"] = processed_features["pixel_attention_mask"][0]
            if "image_sizes" in processed_features:
                output["image_sizes"] = processed_features["image_sizes"][0]

            return output
        else:
            # Invalid format
            available_keys = list(features.keys())
            raise ValueError(
                f"Invalid dataset format for vision models. Expected either:\n"
                f"- MultiDPO format with keys: {multidpo_keys} (plus 'images')\n"
                f"- Standard DPO format with keys: {standard_dpo_keys} (plus 'images')\n"
                f"Got keys: {available_keys}"
            )

    def _set_signature_columns_if_needed(self):
        # If `self.args.remove_unused_columns` is True, non-signature columns are removed.
        # By default, this method sets `self._signature_columns` to the model's expected inputs.
        # In MultiDPOTrainer, we preprocess data, so using the model's signature columns doesn't work.
        # Instead, we set them to the columns expected by `DataCollatorForPreference`, hence the override.
        if self._signature_columns is None:
            # MultiDPO signature columns (includes both old and new formats for compatibility)
            self._signature_columns = [
                # Standard DPO format
                "prompt_input_ids",
                "chosen_input_ids", 
                "rejected_input_ids",
                # MultiDPO format (6-key)
                "chosen_response_input_ids",
                "rejected_response_input_ids", 
                "chosen_prompt_input_ids",
                "rejected_prompt_input_ids",
                "response_input_ids",
                # Vision support
                "image_sizes",
                "pixel_values",
                "pixel_attention_mask",
                # Reference log probs (both formats)
                "ref_chosen_logps",
                "ref_rejected_logps",
                "ref_chosen_logps_dpo",
                "ref_rejected_logps_dpo",
                "ref_chosen_logps_adpo", 
                "ref_rejected_logps_adpo",
            ]

    def get_train_dataloader(self) -> DataLoader:
        """
        Returns the training [`~torch.utils.data.DataLoader`].

        Subclass of transformers.src.transformers.trainer.get_train_dataloader to precompute `ref_log_probs`.
        """

        if self.precompute_ref_log_probs and not self._precomputed_train_ref_log_probs:
            batch_size = self.args.precompute_ref_batch_size or self.args.per_device_train_batch_size
            dataloader_params = {
                "batch_size": batch_size,
                "collate_fn": self.data_collator,
                "num_workers": self.args.dataloader_num_workers,
                "pin_memory": self.args.dataloader_pin_memory,
                "shuffle": False,
            }

            # prepare dataloader
            data_loader = self.accelerator.prepare(DataLoader(self.train_dataset, **dataloader_params))

            # MultiDPO uses 4-part reference logps
            ref_chosen_logps_dpo = []
            ref_rejected_logps_dpo = []
            ref_chosen_logps_adpo = []
            ref_rejected_logps_adpo = []
            
            for padded_batch in tqdm(iterable=data_loader, desc="Train dataset reference log probs"):
                ref_chosen_logp_dpo, ref_rejected_logp_dpo, ref_chosen_logp_adpo, ref_rejected_logp_adpo = self.compute_ref_log_probs(padded_batch)
                ref_chosen_logp_dpo, ref_rejected_logp_dpo, ref_chosen_logp_adpo, ref_rejected_logp_adpo = self.accelerator.gather_for_metrics(
                    (ref_chosen_logp_dpo, ref_rejected_logp_dpo, ref_chosen_logp_adpo, ref_rejected_logp_adpo)
                )
                ref_chosen_logps_dpo.append(ref_chosen_logp_dpo.cpu())
                ref_rejected_logps_dpo.append(ref_rejected_logp_dpo.cpu())
                ref_chosen_logps_adpo.append(ref_chosen_logp_adpo.cpu())
                ref_rejected_logps_adpo.append(ref_rejected_logp_adpo.cpu())

                # Unnecessary cache clearing to avoid OOM
                empty_cache()
                self.accelerator.free_memory()

            all_ref_chosen_logps_dpo = torch.cat(ref_chosen_logps_dpo).float().numpy()
            all_ref_rejected_logps_dpo = torch.cat(ref_rejected_logps_dpo).float().numpy()
            all_ref_chosen_logps_adpo = torch.cat(ref_chosen_logps_adpo).float().numpy()
            all_ref_rejected_logps_adpo = torch.cat(ref_rejected_logps_adpo).float().numpy()

            # Add 4-part reference logps to dataset
            self.train_dataset = self.train_dataset.add_column(name="ref_chosen_logps_dpo", column=all_ref_chosen_logps_dpo)
            self.train_dataset = self.train_dataset.add_column(name="ref_rejected_logps_dpo", column=all_ref_rejected_logps_dpo)
            self.train_dataset = self.train_dataset.add_column(name="ref_chosen_logps_adpo", column=all_ref_chosen_logps_adpo)
            self.train_dataset = self.train_dataset.add_column(name="ref_rejected_logps_adpo", column=all_ref_rejected_logps_adpo)

            self._precomputed_train_ref_log_probs = True

        return super().get_train_dataloader()

    def get_eval_dataloader(self, eval_dataset: Optional[Dataset] = None) -> DataLoader:
        """
        Returns the evaluation [`~torch.utils.data.DataLoader`].

        Subclass of transformers.src.transformers.trainer.get_eval_dataloader to precompute `ref_log_probs`.

        Args:
            eval_dataset (`torch.utils.data.Dataset`, *optional*):
                If provided, will override `self.eval_dataset`. If it is a [`~datasets.Dataset`], columns not accepted
                by the `model.forward()` method are automatically removed. It must implement `__len__`.
        """
        if eval_dataset is None and self.eval_dataset is None:
            raise ValueError("Trainer: evaluation requires an eval_dataset.")
        eval_dataset = eval_dataset if eval_dataset is not None else self.eval_dataset

        if self.precompute_ref_log_probs and not self._precomputed_eval_ref_log_probs:
            batch_size = self.args.precompute_ref_batch_size or self.args.per_device_eval_batch_size
            dataloader_params = {
                "batch_size": batch_size,
                "collate_fn": self.data_collator,
                "num_workers": self.args.dataloader_num_workers,
                "pin_memory": self.args.dataloader_pin_memory,
                "shuffle": False,
            }

            # prepare dataloader
            data_loader = self.accelerator.prepare(DataLoader(eval_dataset, **dataloader_params))

            # MultiDPO uses 4-part reference logps
            ref_chosen_logps_dpo = []
            ref_rejected_logps_dpo = []
            ref_chosen_logps_adpo = []
            ref_rejected_logps_adpo = []
            
            for padded_batch in tqdm(iterable=data_loader, desc="Eval dataset reference log probs"):
                ref_chosen_logp_dpo, ref_rejected_logp_dpo, ref_chosen_logp_adpo, ref_rejected_logp_adpo = self.compute_ref_log_probs(padded_batch)
                ref_chosen_logp_dpo, ref_rejected_logp_dpo, ref_chosen_logp_adpo, ref_rejected_logp_adpo = self.accelerator.gather_for_metrics(
                    (ref_chosen_logp_dpo, ref_rejected_logp_dpo, ref_chosen_logp_adpo, ref_rejected_logp_adpo)
                )
                ref_chosen_logps_dpo.append(ref_chosen_logp_dpo.cpu())
                ref_rejected_logps_dpo.append(ref_rejected_logp_dpo.cpu())
                ref_chosen_logps_adpo.append(ref_chosen_logp_adpo.cpu())
                ref_rejected_logps_adpo.append(ref_rejected_logp_adpo.cpu())

            all_ref_chosen_logps_dpo = torch.cat(ref_chosen_logps_dpo).float().numpy()
            all_ref_rejected_logps_dpo = torch.cat(ref_rejected_logps_dpo).float().numpy()
            all_ref_chosen_logps_adpo = torch.cat(ref_chosen_logps_adpo).float().numpy()
            all_ref_rejected_logps_adpo = torch.cat(ref_rejected_logps_adpo).float().numpy()

            # Add 4-part reference logps to dataset
            eval_dataset = eval_dataset.add_column(name="ref_chosen_logps_dpo", column=all_ref_chosen_logps_dpo)
            eval_dataset = eval_dataset.add_column(name="ref_rejected_logps_dpo", column=all_ref_rejected_logps_dpo)
            eval_dataset = eval_dataset.add_column(name="ref_chosen_logps_adpo", column=all_ref_chosen_logps_adpo)
            eval_dataset = eval_dataset.add_column(name="ref_rejected_logps_adpo", column=all_ref_rejected_logps_adpo)

            # Save calculated ref_chosen_logps and ref_rejected_logps to the eval_dataset for subsequent runs
            if self.eval_dataset is not None:
                self.eval_dataset = eval_dataset
            self._precomputed_eval_ref_log_probs = True

        return super().get_eval_dataloader(eval_dataset=eval_dataset)

    @contextmanager
    def null_ref_context(self):
        """Context manager for handling null reference model (that is, peft adapter manipulation)."""
        with (
            self.accelerator.unwrap_model(self.model).disable_adapter()
            if self.is_peft_model and not self.ref_adapter_name
            else nullcontext()
        ):
            if self.ref_adapter_name:
                self.model.set_adapter(self.ref_adapter_name)
            yield
            if self.ref_adapter_name:
                self.model.set_adapter(self.model_adapter_name or "default")

    def compute_ref_log_probs(self, batch: dict[str, torch.LongTensor]) -> dict:
        """Computes log probabilities of the reference model for a single padded batch of a DPO specific dataset."""
        compte_ref_context_manager = (
            autocast(self.accelerator.device.type) if self._peft_has_been_casted_to_bf16 else nullcontext()
        )
        with torch.no_grad(), compte_ref_context_manager:
            if self.ref_model is None:
                with self.null_ref_context():
                    ref_model_output = self.concatenated_forward(self.model, batch, is_ref_model=True)
            else:
                ref_model_output = self.concatenated_forward(self.ref_model, batch, is_ref_model=True)
        return (
            ref_model_output["chosen_logps_dpo"], 
            ref_model_output["rejected_logps_dpo"],
            ref_model_output["chosen_logps_adpo"], 
            ref_model_output["rejected_logps_adpo"]
        )

    @staticmethod
    def concatenated_inputs(
        batch: dict[str, Union[list, torch.LongTensor]], padding_value: int
    ) -> dict[str, torch.LongTensor]:
        """
        Concatenate the `chosen` and `rejected` inputs from the batch into a single tensor for both the prompt and
        completion sequences.

        Args:
            batch (`dict[str, Union[list, torch.LongTensor]]`):
                A batch of input data. The batch must contain the following keys:

                - `"prompt_input_ids"`: Tensor of shape `(batch_size, prompt_length)` representing the prompt input
                  IDs.
                - `"chosen_response_input_ids"`: Tensor of shape `(batch_size, chosen_length)` representing the chosen
                  completion input IDs.
                - `"rejected_response_input_ids"`: Tensor of shape `(batch_size, rejected_length)` representing the rejected
                  completion input IDs.
                - `"response_input_ids"`
                - `"chosen_prompt_input_ids"`
                - `"rejected_prompt_input_ids"`
                - `"prompt_pixel_values"` (optional): Tensor for pixel values, if available.
                - `"prompt_pixel_attention_mask"` (optional): Tensor for pixel attention masks, if available.

            padding_value (`int`):
                The padding value to use for the concatenated completion sequences (`chosen_input_ids` and
                `rejected_input_ids`).

        Returns:
            `dict[str, torch.LongTensor]`: A dictionary containing:

                - `"prompt_input_ids"`: Concatenated prompt input IDs of shape `(2 * batch_size, prompt_length)`.
                - `"completion_input_ids"`: Concatenated chosen and rejected completion input IDs of shape `(2 *
                  batch_size, max_completion_length)`.
                - `"prompt_attention_mask"`: Concatenated prompt attention masks of shape `(2 * batch_size,
                  prompt_length)`.
                - `"completion_attention_mask"`: Concatenated chosen and rejected attention masks of shape `(2 *
                  batch_size, max_completion_length)`.
                - `"pixel_values"` (optional): Concatenated pixel values if `"prompt_pixel_values"` are present.
                - `"pixel_attention_mask"` (optional): Concatenated pixel attention masks if
                  `"prompt_pixel_attention_mask"` are present.

        Notes:
            The completion input IDs and attention masks are padded to the maximum completion length of the chosen or
            rejected sequences.
        """
        output = {}

        # Concatenate four parts: prompt, prompt, chosen_prompt, rejected_prompt (with padding)
        max_prompt_length = max(
            batch["prompt_input_ids"].shape[1], 
            batch["chosen_prompt_input_ids"].shape[1], 
            batch["rejected_prompt_input_ids"].shape[1]  # Fixed: was "rejected_input_ids"
        )
        
        # Pad all prompt tensors to max_prompt_length and concatenate four parts
        padded_prompt_ids = pad_to_length(batch["prompt_input_ids"], max_prompt_length, pad_value=padding_value)
        padded_chosen_prompt_ids = pad_to_length(batch["chosen_prompt_input_ids"], max_prompt_length, pad_value=padding_value)
        padded_rejected_prompt_ids = pad_to_length(batch["rejected_prompt_input_ids"], max_prompt_length, pad_value=padding_value)
        
        output["prompt_input_ids"] = torch.cat([
            padded_prompt_ids, 
            padded_prompt_ids,
            padded_chosen_prompt_ids, 
            padded_rejected_prompt_ids
        ], dim=0)
        
        # Do the same for attention masks (pad with 0)
        padded_prompt_mask = pad_to_length(batch["prompt_attention_mask"], max_prompt_length, pad_value=0)
        padded_chosen_prompt_mask = pad_to_length(batch["chosen_prompt_attention_mask"], max_prompt_length, pad_value=0)
        padded_rejected_prompt_mask = pad_to_length(batch["rejected_prompt_attention_mask"], max_prompt_length, pad_value=0)
        
        output["prompt_attention_mask"] = torch.cat([
            padded_prompt_mask, 
            padded_prompt_mask,
            padded_chosen_prompt_mask, 
            padded_rejected_prompt_mask
        ], dim=0)
        if "pixel_values" in batch:
            output["pixel_values"] = torch.cat([batch["pixel_values"], batch["pixel_values"], batch["pixel_values"], batch["pixel_values"]], dim=0)

        if "pixel_attention_mask" in batch:
            output["pixel_attention_mask"] = torch.cat(
                [batch["pixel_attention_mask"], batch["pixel_attention_mask"], batch["pixel_attention_mask"], batch["pixel_attention_mask"]], dim=0
            )
        if "image_sizes" in batch:
            output["image_sizes"] = torch.cat([batch["image_sizes"], batch["image_sizes"], batch["image_sizes"], batch["image_sizes"]], dim=0)

        # Concatenate four parts: chosen_response, rejected_response, response, response
        max_completion_length = max(
            batch["chosen_response_input_ids"].shape[1], 
            batch["rejected_response_input_ids"].shape[1],
            batch["response_input_ids"].shape[1]
        )
        
        # Pad all completion tensors to max_completion_length and concatenate four parts
        padded_chosen_response = pad_to_length(batch["chosen_response_input_ids"], max_completion_length, pad_value=padding_value)
        padded_rejected_response = pad_to_length(batch["rejected_response_input_ids"], max_completion_length, pad_value=padding_value)
        padded_response = pad_to_length(batch["response_input_ids"], max_completion_length, pad_value=padding_value)
        
        output["completion_input_ids"] = torch.cat([
            padded_chosen_response, 
            padded_rejected_response,
            padded_response, 
            padded_response
        ], dim=0)
        
        # Do the same for attention masks (pad with 0)
        padded_chosen_response_mask = pad_to_length(batch["chosen_response_attention_mask"], max_completion_length, pad_value=0)
        padded_rejected_response_mask = pad_to_length(batch["rejected_response_attention_mask"], max_completion_length, pad_value=0)
        padded_response_mask = pad_to_length(batch["response_attention_mask"], max_completion_length, pad_value=0)
        
        output["completion_attention_mask"] = torch.cat([
            padded_chosen_response_mask,
            padded_rejected_response_mask, 
            padded_response_mask,
            padded_response_mask
        ], dim=0)

        return output

    def multidpo_loss(
        self,
        chosen_logps_dpo: torch.FloatTensor,
        rejected_logps_dpo: torch.FloatTensor,
        chosen_logps_adpo: torch.FloatTensor,
        rejected_logps_adpo: torch.FloatTensor,
        ref_chosen_logps_dpo: torch.FloatTensor,
        ref_rejected_logps_dpo: torch.FloatTensor,
        ref_chosen_logps_adpo: torch.FloatTensor,
        ref_rejected_logps_adpo: torch.FloatTensor,
    ) -> tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """
        Compute the combined MultiDPO loss: λ * DPO_loss + (1-λ) * ADPO_loss

        Args:
            chosen_logps_dpo (`torch.FloatTensor`): Policy logps for chosen_response given prompt
            rejected_logps_dpo (`torch.FloatTensor`): Policy logps for rejected_response given prompt  
            chosen_logps_adpo (`torch.FloatTensor`): Policy logps for response given chosen_prompt
            rejected_logps_adpo (`torch.FloatTensor`): Policy logps for response given rejected_prompt
            ref_chosen_logps_dpo (`torch.FloatTensor`): Reference logps for chosen_response given prompt
            ref_rejected_logps_dpo (`torch.FloatTensor`): Reference logps for rejected_response given prompt
            ref_chosen_logps_adpo (`torch.FloatTensor`): Reference logps for response given chosen_prompt  
            ref_rejected_logps_adpo (`torch.FloatTensor`): Reference logps for response given rejected_prompt

        Returns:
            A tuple of three tensors: `(combined_losses, combined_chosen_rewards, combined_rejected_rewards)`.
        """
        # Compute DPO loss: chosen_response vs rejected_response (given prompt)
        dpo_losses, dpo_chosen_rewards, dpo_rejected_rewards = self.dpo_loss(
            chosen_logps_dpo, rejected_logps_dpo, ref_chosen_logps_dpo, ref_rejected_logps_dpo
        )
        
        # Compute ADPO loss: response given chosen_prompt vs response given rejected_prompt  
        adpo_losses, adpo_chosen_rewards, adpo_rejected_rewards = self.dpo_loss(
            chosen_logps_adpo, rejected_logps_adpo, ref_chosen_logps_adpo, ref_rejected_logps_adpo
        )
        
        # Combine losses using lambda_weight: λ * DPO_loss + (1-λ) * ADPO_loss
        lambda_weight = self.lambda_weight
        combined_losses = lambda_weight * dpo_losses + (1 - lambda_weight) * adpo_losses
        
        # Debug loss components (every 5 steps to avoid spam)
        if hasattr(self, 'state') and self.state.global_step % 5 == 0:
            if hasattr(self, 'accelerator') and self.accelerator.is_main_process:
                dpo_loss_mean = dpo_losses.mean().item()
                adpo_loss_mean = adpo_losses.mean().item()
                combined_loss_mean = combined_losses.mean().item()
                
                print(f"\n🧮 Loss Debug (Step {self.state.global_step}):")
                print(f"  🔷 DPO Loss: {dpo_loss_mean:.6f}")
                print(f"  🔶 ADPO Loss: {adpo_loss_mean:.6f}")
                print(f"  ⚖️  Lambda weight: {lambda_weight:.3f}")
                print(f"  🔸 Combined Loss: {combined_loss_mean:.6f}")
                print(f"  📏 DPO contribution: {lambda_weight * dpo_loss_mean:.6f}")
                print(f"  📐 ADPO contribution: {(1-lambda_weight) * adpo_loss_mean:.6f}")
                
                # Check if ADPO is getting suppressed
                if adpo_loss_mean > dpo_loss_mean * 10:
                    print("⚠️  ADPO loss much larger than DPO - possible scaling issue!")
                elif adpo_loss_mean < dpo_loss_mean / 10:
                    print("⚠️  ADPO loss much smaller than DPO - possible underweighting!")
                
                # Debug logp ranges
                print(f"  📊 DPO logps: chosen={chosen_logps_dpo.mean().item():.2f}, rejected={rejected_logps_dpo.mean().item():.2f}")
                print(f"  📊 ADPO logps: chosen={chosen_logps_adpo.mean().item():.2f}, rejected={rejected_logps_adpo.mean().item():.2f}")
        combined_chosen_rewards = lambda_weight * dpo_chosen_rewards + (1 - lambda_weight) * adpo_chosen_rewards
        combined_rejected_rewards = lambda_weight * dpo_rejected_rewards + (1 - lambda_weight) * adpo_rejected_rewards
        
        return combined_losses, combined_chosen_rewards, combined_rejected_rewards

    def dpo_loss(
        self,
        chosen_logps: torch.FloatTensor,
        rejected_logps: torch.FloatTensor,
        ref_chosen_logps: torch.FloatTensor,
        ref_rejected_logps: torch.FloatTensor,
    ) -> tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """
        Compute the DPO loss for a batch of policy and reference model log probabilities.
        This is the original DPO loss function, now used as a helper for MultiDPO.

        Args:
            chosen_logps (`torch.FloatTensor`):
                Log probabilities of the model for the chosen responses. Shape: `(batch_size,)`.
            rejected_logps (`torch.FloatTensor`):
                Log probabilities of the model for the rejected responses. Shape: `(batch_size,)`.
            ref_chosen_logps (`torch.FloatTensor`):
                Log probabilities of the reference model for the chosen responses. Shape: `(batch_size,)`.
            ref_rejected_logps (`torch.FloatTensor`):
                Log probabilities of the reference model for the rejected responses Shape: `(batch_size,)`.

        Returns:
            A tuple of three tensors: `(losses, chosen_rewards, rejected_rewards)`. The losses tensor contains the DPO
            loss for each example in the batch. The `chosen_rewards` and `rejected_rewards` tensors contain the rewards
            for the chosen and rejected responses, respectively.
        """
        device = self.accelerator.device

        # Get the log ratios for the chosen and rejected responses
        chosen_logratios = chosen_logps.to(device) - (not self.reference_free) * ref_chosen_logps.to(device)
        rejected_logratios = rejected_logps.to(device) - (not self.reference_free) * ref_rejected_logps.to(device)

        if self.f_divergence_type == FDivergenceType.ALPHA_DIVERGENCE.value:
            # The alpha-divergence formula: (1 - u^-alpha) / alpha
            # The divergence difference between the chosen and rejected sample is:
            #     (1 - u[w]^-alpha) / alpha - (1 - u[l]^-alpha) / alpha
            #        = (u[l]^-alpha - u[w]^-alpha) / alpha
            # where u[w] and u[l] are the policy/reference probability ratios
            # for the chosen and rejected samples, respectively.
            alpha_coef = FDivergenceConstants.ALPHA_DIVERGENCE_COEF_DEFAULT
            if self.f_divergence_params and FDivergenceConstants.ALPHA_DIVERGENCE_COEF_KEY in self.f_divergence_params:
                alpha_coef = float(self.f_divergence_params[FDivergenceConstants.ALPHA_DIVERGENCE_COEF_KEY])
            logits = (cap_exp(rejected_logratios * -alpha_coef) - cap_exp(chosen_logratios * -alpha_coef)) / alpha_coef
        else:
            logratios = chosen_logps - rejected_logps
            if self.reference_free:
                ref_logratios = torch.tensor([0], dtype=logratios.dtype, device=logratios.device)
            else:
                ref_logratios = ref_chosen_logps - ref_rejected_logps

            logratios = logratios.to(self.accelerator.device)
            ref_logratios = ref_logratios.to(self.accelerator.device)
            logits = logratios - ref_logratios

            if self.f_divergence_type == FDivergenceType.JS_DIVERGENCE.value:
                # The js-divergence formula: log(2 * u / (1 + u))
                # The divergence difference between the chosen and rejected sample is:
                #     log(2 * u[w] / (1 + u[w])) - log(2 * u[l] / (1 + u[l]))
                #       = log(u[w]) - log(u[l]) - (log(1 + u[w]) - log(1 + u[l]))
                # where u[w] and u[l] are the policy/reference probability ratios
                # for the chosen and rejected samples, respectively.
                logits -= F.softplus(chosen_logratios) - F.softplus(rejected_logratios)

        # The beta is a temperature parameter for the DPO loss, typically something in the range of 0.1 to 0.5.
        # We ignore the reference model as beta -> 0. The label_smoothing parameter encodes our uncertainty about the
        # labels and calculates a conservative DPO loss.
        if self.loss_type == "sigmoid":
            losses = (
                -F.logsigmoid(self.beta * logits) * (1 - self.label_smoothing)
                - F.logsigmoid(-self.beta * logits) * self.label_smoothing
            )

        elif self.loss_type == "robust":
            losses = (
                -F.logsigmoid(self.beta * logits) * (1 - self.label_smoothing)
                + F.logsigmoid(-self.beta * logits) * self.label_smoothing
            ) / (1 - 2 * self.label_smoothing)

        elif self.loss_type == "exo_pair":
            # eqn (16) of the EXO paper: https://huggingface.co/papers/2402.00856
            import math

            if self.label_smoothing == 0:
                self.label_smoothing = 1e-3
            losses = (self.beta * logits).sigmoid() * (
                F.logsigmoid(self.beta * logits) - math.log(1 - self.label_smoothing)
            ) + (-self.beta * logits).sigmoid() * (F.logsigmoid(-self.beta * logits) - math.log(self.label_smoothing))

        elif self.loss_type == "hinge":
            losses = torch.relu(1 - self.beta * logits)

        elif self.loss_type == "ipo":
            # eqn (17) of the paper where beta is the regularization parameter for the IPO loss, denoted by tau in the paper.
            losses = (logits - 1 / (2 * self.beta)) ** 2

        elif self.loss_type == "bco_pair":
            chosen_logratios = chosen_logps - ref_chosen_logps
            rejected_logratios = rejected_logps - ref_rejected_logps
            chosen_rewards = self.beta * chosen_logratios
            rejected_rewards = self.beta * rejected_logratios
            rewards = torch.cat((chosen_rewards, rejected_rewards), 0).mean().detach()
            self.running.update(rewards)
            delta = self.running.mean
            losses = -F.logsigmoid((self.beta * chosen_logratios) - delta) - F.logsigmoid(
                -(self.beta * rejected_logratios - delta)
            )

        elif self.loss_type == "sppo_hard":
            # In the paper (https://huggingface.co/papers/2405.00675), SPPO employs a soft probability approach,
            # estimated using the PairRM score. The probability calculation is conducted outside of the trainer class.
            # The version described here is the hard probability version, where P in Equation (4.7) of Algorithm 1 is
            # set to 1 for the winner and 0 for the loser.
            a = chosen_logps - ref_chosen_logps
            b = rejected_logps - ref_rejected_logps
            losses = (a - 0.5 / self.beta) ** 2 + (b + 0.5 / self.beta) ** 2

        elif self.loss_type == "nca_pair":
            chosen_rewards = (chosen_logps - ref_chosen_logps) * self.beta
            rejected_rewards = (rejected_logps - ref_rejected_logps) * self.beta
            losses = (
                -F.logsigmoid(chosen_rewards)
                - 0.5 * F.logsigmoid(-chosen_rewards)
                - 0.5 * F.logsigmoid(-rejected_rewards)
            )

        elif self.loss_type == "aot_pair":
            chosen_logratios = chosen_logps - ref_chosen_logps
            rejected_logratios = rejected_logps - ref_rejected_logps
            chosen_logratios_sorted, _ = torch.sort(chosen_logratios, dim=0)
            rejected_logratios_sorted, _ = torch.sort(rejected_logratios, dim=0)
            delta = chosen_logratios_sorted - rejected_logratios_sorted
            losses = (
                -F.logsigmoid(self.beta * delta) * (1 - self.label_smoothing)
                - F.logsigmoid(-self.beta * delta) * self.label_smoothing
            )

        elif self.loss_type == "aot":
            logratios = chosen_logps - rejected_logps
            ref_logratios = ref_chosen_logps - ref_rejected_logps
            logratios_sorted, _ = torch.sort(logratios, dim=0)
            ref_logratios_sorted, _ = torch.sort(ref_logratios, dim=0)
            delta = logratios_sorted - ref_logratios_sorted
            losses = (
                -F.logsigmoid(self.beta * delta) * (1 - self.label_smoothing)
                - F.logsigmoid(-self.beta * delta) * self.label_smoothing
            )

        elif self.loss_type == "apo_zero":
            # Eqn (7) of the APO paper (https://huggingface.co/papers/2408.06266)
            # Use this loss when you believe the chosen outputs are better than your model's default output
            losses_chosen = 1 - F.sigmoid(self.beta * chosen_logratios)  # Increase chosen likelihood
            losses_rejected = F.sigmoid(self.beta * rejected_logratios)  # Decrease rejected likelihood
            losses = losses_chosen + losses_rejected

        elif self.loss_type == "apo_down":
            # Eqn (8) of the APO paper (https://huggingface.co/papers/2408.06266)
            # Use this loss when you believe the chosen outputs are worse than your model's default output.
            # Decrease chosen likelihood and decrease rejected likelihood more
            losses_chosen = F.sigmoid(self.beta * chosen_logratios)
            losses_rejected = 1 - F.sigmoid(self.beta * (chosen_logratios - rejected_logratios))
            losses = losses_chosen + losses_rejected

        elif self.loss_type == "discopop":
            # Eqn (5) of the DiscoPOP paper (https://huggingface.co/papers/2406.08414)
            # This loss was discovered with LLM discovery
            logratios = chosen_logps - rejected_logps
            ref_logratios = ref_chosen_logps - ref_rejected_logps
            logits = logratios - ref_logratios
            logits = logits * self.beta
            # Modulate the mixing coefficient based on the log ratio magnitudes
            log_ratio_modulation = torch.sigmoid(logits / self.args.discopop_tau)
            logistic_component = -F.logsigmoid(logits)
            exp_component = torch.exp(-logits)
            # Blend between logistic and exponential component based on log ratio modulation
            losses = logistic_component * (1 - log_ratio_modulation) + exp_component * log_ratio_modulation

        else:
            raise ValueError(
                f"Unknown loss type: {self.loss_type}. Should be one of ['sigmoid', 'hinge', 'ipo', 'exo_pair', "
                "'nca_pair', 'robust', 'bco_pair', 'sppo_hard', 'aot', 'aot_pair', 'discopop', 'apo_zero', 'apo_down']"
            )

        chosen_rewards = self.beta * (chosen_logps.to(device) - ref_chosen_logps.to(device)).detach()
        rejected_rewards = self.beta * (rejected_logps.to(device) - ref_rejected_logps.to(device)).detach()

        return losses, chosen_rewards, rejected_rewards

    def _compute_loss_liger(self, model: nn.Module, batch: dict[str, Union[list, torch.LongTensor]]):
        unwrapped_model = self.accelerator.unwrap_model(model)
        concatenated_batch = self.concatenated_inputs(batch, padding_value=self.padding_value)

        model_kwargs = {}
        if self.aux_loss_enabled:
            model_kwargs["output_router_logits"] = True

        # Add the pixel values and attention masks for vision models
        if "pixel_values" in concatenated_batch:
            model_kwargs["pixel_values"] = concatenated_batch["pixel_values"]
        if "pixel_attention_mask" in concatenated_batch:
            model_kwargs["pixel_attention_mask"] = concatenated_batch["pixel_attention_mask"]
        if "image_sizes" in concatenated_batch:
            model_kwargs["image_sizes"] = concatenated_batch["image_sizes"]

        prompt_attention_mask = concatenated_batch["prompt_attention_mask"]
        completion_attention_mask = concatenated_batch["completion_attention_mask"]

        if self.is_encoder_decoder:
            # 1. Get encoder outputs
            encoder_outputs = unwrapped_model.get_encoder()(
                concatenated_batch["prompt_input_ids"],
                attention_mask=concatenated_batch["prompt_attention_mask"],
                return_dict=True,
            )
            # 2. Prepare decoder inputs
            decoder_input_ids = shift_tokens_right(
                concatenated_batch["completion_input_ids"],
                unwrapped_model.config.decoder_start_token_id,
            )
            # 3. Get decoder outputs
            decoder_outputs = unwrapped_model.get_decoder()(
                input_ids=decoder_input_ids,
                attention_mask=concatenated_batch["completion_attention_mask"],
                encoder_hidden_states=encoder_outputs.last_hidden_state,
                encoder_attention_mask=concatenated_batch["prompt_attention_mask"],
                use_cache=False,
            )
            hidden_states = decoder_outputs.last_hidden_state

            ref_hidden_states = None
            if not self.reference_free and self.ref_model is not None:
                unwrapped_ref_model = self.accelerator.unwrap_model(self.ref_model)
                ref_encoder_outputs = unwrapped_ref_model.get_encoder()(
                    concatenated_batch["prompt_input_ids"],
                    attention_mask=concatenated_batch["prompt_attention_mask"],
                    return_dict=True,
                )
                ref_decoder_outputs = unwrapped_ref_model.get_decoder()(
                    input_ids=decoder_input_ids,
                    attention_mask=concatenated_batch["completion_attention_mask"],
                    encoder_hidden_states=ref_encoder_outputs.last_hidden_state,
                    encoder_attention_mask=concatenated_batch["prompt_attention_mask"],
                    use_cache=False,
                )
                ref_hidden_states = ref_decoder_outputs.last_hidden_state
            elif not self.reference_free:
                with self.null_ref_context():
                    ref_encoder_outputs = unwrapped_model.get_encoder()(
                        concatenated_batch["prompt_input_ids"],
                        attention_mask=concatenated_batch["prompt_attention_mask"],
                        return_dict=True,
                    )
                    ref_decoder_outputs = unwrapped_model.get_decoder()(
                        input_ids=decoder_input_ids,
                        attention_mask=concatenated_batch["completion_attention_mask"],
                        encoder_hidden_states=ref_encoder_outputs.last_hidden_state,
                        encoder_attention_mask=concatenated_batch["prompt_attention_mask"],
                        use_cache=False,
                    )
                    ref_hidden_states = ref_decoder_outputs.last_hidden_state

            labels = concatenated_batch["completion_input_ids"]
            loss_mask = completion_attention_mask.bool()
        else:
            # For decoder-only models
            input_ids = torch.cat(
                (concatenated_batch["prompt_input_ids"], concatenated_batch["completion_input_ids"]), dim=1
            )
            attention_mask = torch.cat(
                (concatenated_batch["prompt_attention_mask"], concatenated_batch["completion_attention_mask"]),
                dim=1,
            )
            # Mask the prompt but not the completion for the loss
            loss_mask = torch.cat(
                (torch.zeros_like(prompt_attention_mask), completion_attention_mask),
                dim=1,
            )

            # Flush and truncate
            if self.max_length is not None and self.max_length < attention_mask.size(1):
                if self.truncation_mode == "keep_start":
                    # Flush left to reduce the memory usage
                    # [[0, 0, x, x, x, x],  ->  [[x, x, x, x],
                    #  [0, x, x, x, 0, 0]]       [x, x, x, 0]]
                    attention_mask, input_ids, loss_mask = flush_left(attention_mask, input_ids, loss_mask)
                    attention_mask = attention_mask[:, : self.max_length]
                    input_ids = input_ids[:, : self.max_length]
                    loss_mask = loss_mask[:, : self.max_length]
                elif self.truncation_mode == "keep_end":
                    # Flush right before truncating left, then flush left
                    # [[0, 0, x, x, x, x],  ->  [[0, 0, x, x],
                    #  [0, x, x, x, 0, 0]]       [0, x, x, x]]
                    attention_mask, input_ids, loss_mask = flush_right(attention_mask, input_ids, loss_mask)
                    input_ids = input_ids[:, -self.max_length :]
                    attention_mask = attention_mask[:, -self.max_length :]
                    loss_mask = loss_mask[:, -self.max_length :]
                    attention_mask, input_ids, loss_mask = flush_left(attention_mask, input_ids, loss_mask)
                else:
                    raise ValueError(
                        f"Unknown truncation mode: '{self.truncation_mode}'. Should be one of ['keep_end', "
                        "'keep_start']."
                    )
            else:
                # Flush left to reduce the memory usage
                # [[0, 0, x, x, x, x],  ->  [[x, x, x, x],
                #  [0, x, x, x, 0, 0]]       [x, x, x, 0]]
                attention_mask, input_ids, loss_mask = flush_left(attention_mask, input_ids, loss_mask)

            # Add logits_to_keep optimization
            if self.use_logits_to_keep:
                first_compute_index = loss_mask.nonzero(as_tuple=True)[1].min()
                logits_to_keep = (loss_mask.shape[1] - first_compute_index).item() + 1
                model_kwargs["logits_to_keep"] = logits_to_keep

            model_kwargs["output_hidden_states"] = True

            # Add padding-free training support
            if self.padding_free:
                input_ids = input_ids[attention_mask.bool()].unsqueeze(0)
                loss_mask = loss_mask[attention_mask.bool()].unsqueeze(0)
                position_ids = attention_mask.cumsum(1)[attention_mask.bool()].unsqueeze(0) - 1
                model_kwargs["position_ids"] = position_ids
            else:
                model_kwargs["attention_mask"] = attention_mask

            # Get the base model outputs (before LM head)
            if hasattr(unwrapped_model, "get_decoder"):
                base_model = unwrapped_model.get_decoder()
            else:
                base_model = getattr(unwrapped_model, self.args.base_model_attribute_name, unwrapped_model)

            outputs = base_model(
                input_ids,
                use_cache=False,
                **model_kwargs,
            )
            hidden_states = outputs.last_hidden_state[:, :-1]

            # Get reference hidden states if needed
            ref_hidden_states = None
            if not self.reference_free and self.ref_model is not None:
                unwrapped_ref_model = self.accelerator.unwrap_model(self.ref_model)
                if hasattr(unwrapped_ref_model, "get_decoder"):
                    ref_base_model = unwrapped_ref_model.get_decoder()
                else:
                    ref_base_model = getattr(
                        unwrapped_ref_model, self.args.base_model_attribute_name, unwrapped_ref_model
                    )

                ref_outputs = ref_base_model(
                    input_ids,
                    use_cache=False,
                    **model_kwargs,
                )
                ref_hidden_states = ref_outputs.last_hidden_state[:, :-1]
            elif not self.reference_free:
                if hasattr(unwrapped_model, "get_decoder"):
                    ref_base_model = unwrapped_model.get_decoder()
                else:
                    ref_base_model = getattr(unwrapped_model, self.args.base_model_attribute_name, unwrapped_model)
                with self.null_ref_context():
                    ref_outputs = ref_base_model(
                        input_ids,
                        attention_mask=attention_mask,
                        use_cache=False,
                        **model_kwargs,
                    )
                    ref_hidden_states = ref_outputs.last_hidden_state[:, :-1]

            masked_input_ids = torch.where(loss_mask != 0, input_ids, self.label_pad_token_id)
            labels = masked_input_ids[:, 1:]  # Shift right for casual LM

        # Get the LM head
        lm_head = unwrapped_model.get_output_embeddings()

        # Get reference model weights if needed
        ref_weight = None
        ref_bias = None
        if not self.reference_free:
            if self.ref_model is not None:
                unwrapped_ref_model = self.accelerator.unwrap_model(self.ref_model)
                ref_lm_head = unwrapped_ref_model.get_output_embeddings()
            else:
                with self.null_ref_context():
                    ref_lm_head = unwrapped_model.get_output_embeddings()
            ref_weight = ref_lm_head.weight
            ref_bias = ref_lm_head.bias if hasattr(ref_lm_head, "bias") else None

        # Compute loss using Liger kernel
        loss_output = self.dpo_loss_fn(
            lm_head.weight,
            hidden_states,
            labels,
            bias=lm_head.bias if hasattr(lm_head, "bias") else None,
            ref_input=ref_hidden_states if not self.reference_free else None,
            ref_weight=ref_weight if not self.reference_free else None,
            ref_bias=ref_bias if not self.reference_free else None,
        )
        (
            loss,
            (chosen_logps, rejected_logps, chosen_logits_mean, rejected_logits_mean, nll_loss, *aux_outputs),
        ) = loss_output

        output = {
            "loss": loss,
            "chosen_logps": chosen_logps,
            "rejected_logps": rejected_logps,
            "mean_chosen_logits": chosen_logits_mean,
            "mean_rejected_logits": rejected_logits_mean,
            "nll_loss": nll_loss,
            "chosen_rewards": aux_outputs[0],
            "rejected_rewards": aux_outputs[1],
        }
        if self.aux_loss_enabled:
            output["aux_loss"] = outputs.aux_loss

        return output

    def concatenated_forward(
        self, model: nn.Module, batch: dict[str, Union[list, torch.LongTensor]], is_ref_model: bool = False
    ):
        """
        Runs the given model on the given batch of inputs, concatenating the chosen and rejected inputs together.

        We do this to avoid doing two forward passes, because it's faster for FSDP.

        Args:
            model:
                Model to run the forward pass on.
            batch:
                Batch of input data.
            is_ref_model:
                Whether this method is being called for the reference model. If `True`, length desensitization is not
                applied.
        """
        num_examples = batch["prompt_input_ids"].shape[0]

        concatenated_batch = self.concatenated_inputs(batch, padding_value=self.padding_value)

        model_kwargs = {"use_cache": False}
        if self.aux_loss_enabled:
            model_kwargs["output_router_logits"] = True

        # Add the pixel values and attention masks for vision models
        if "pixel_values" in concatenated_batch:
            model_kwargs["pixel_values"] = concatenated_batch["pixel_values"]
        if "pixel_attention_mask" in concatenated_batch:
            model_kwargs["pixel_attention_mask"] = concatenated_batch["pixel_attention_mask"]
        if "image_sizes" in concatenated_batch:
            model_kwargs["image_sizes"] = concatenated_batch["image_sizes"]

        prompt_input_ids = concatenated_batch["prompt_input_ids"]
        prompt_attention_mask = concatenated_batch["prompt_attention_mask"]
        completion_input_ids = concatenated_batch["completion_input_ids"]
        completion_attention_mask = concatenated_batch["completion_attention_mask"]
        if self.is_encoder_decoder:
            labels = completion_input_ids
            labels[completion_attention_mask == 0] = self.label_pad_token_id
            outputs = model(
                input_ids=prompt_input_ids,
                attention_mask=prompt_attention_mask,
                labels=labels,  # we need the labels for the logits to be returned
                **model_kwargs,
            )
            logits = outputs.logits
            loss_mask = completion_attention_mask.bool()
        else:
            # Concatenate the prompt and completion inputs
            input_ids = torch.cat((prompt_input_ids, completion_input_ids), dim=1)
            attention_mask = torch.cat((prompt_attention_mask, completion_attention_mask), dim=1)
            # Mask the prompt but not the completion for the loss
            loss_mask = torch.cat(
                (torch.zeros_like(prompt_attention_mask), completion_attention_mask),
                dim=1,
            )

            # Flush and truncate
            if self.max_length is not None and self.max_length < attention_mask.size(1):
                if self.truncation_mode == "keep_start":
                    # Flush left to reduce the memory usage
                    # [[0, 0, x, x, x, x],  ->  [[x, x, x, x],
                    #  [0, x, x, x, 0, 0]]       [x, x, x, 0]]
                    attention_mask, input_ids, loss_mask = flush_left(attention_mask, input_ids, loss_mask)
                    attention_mask = attention_mask[:, : self.max_length]
                    input_ids = input_ids[:, : self.max_length]
                    loss_mask = loss_mask[:, : self.max_length]
                elif self.truncation_mode == "keep_end":
                    # Flush right before truncating left, then flush left
                    # [[0, 0, x, x, x, x],  ->  [[0, 0, x, x],
                    #  [0, x, x, x, 0, 0]]       [0, x, x, x]]
                    attention_mask, input_ids, loss_mask = flush_right(attention_mask, input_ids, loss_mask)
                    input_ids = input_ids[:, -self.max_length :]
                    attention_mask = attention_mask[:, -self.max_length :]
                    loss_mask = loss_mask[:, -self.max_length :]
                    attention_mask, input_ids, loss_mask = flush_left(attention_mask, input_ids, loss_mask)
                else:
                    raise ValueError(
                        f"Unknown truncation mode: '{self.truncation_mode}'. Should be one of ['keep_end', "
                        "'keep_start']."
                    )
            else:
                # Flush left to reduce the memory usage
                # [[0, 0, x, x, x, x],  ->  [[x, x, x, x],
                #  [0, x, x, x, 0, 0]]       [x, x, x, 0]]
                attention_mask, input_ids, loss_mask = flush_left(attention_mask, input_ids, loss_mask)

            if self.use_logits_to_keep:
                # Compute logits_to_keep based on loss_mask pattern:
                # [[0, 0, 0, x, x, x, x],
                #  [0, 0, 0, x, x, x, 0]]
                #         ^ start computing logits from here ([:, -(7-3+1):])
                first_compute_index = loss_mask.nonzero(as_tuple=True)[1].min()
                logits_to_keep = (loss_mask.shape[1] - first_compute_index).item() + 1  # +1 for the first label
                model_kwargs["logits_to_keep"] = logits_to_keep

            model_kwargs["output_hidden_states"] = True

            if self.padding_free:
                # Flatten the input_ids, position_ids, and loss_mask
                # input_ids = [[a, b, c, 0], ->     input_ids = [[a, b, c, d, e, f, g]]
                #              [d, e, f, g]]     position_ids = [[0, 1, 2, 0, 1, 2, 3]]
                input_ids = input_ids[attention_mask.bool()].unsqueeze(0)
                loss_mask = loss_mask[attention_mask.bool()].unsqueeze(0)
                position_ids = attention_mask.cumsum(1)[attention_mask.bool()].unsqueeze(0) - 1
                model_kwargs["position_ids"] = position_ids
            else:
                model_kwargs["attention_mask"] = attention_mask

            outputs = model(input_ids, **model_kwargs)
            logits = outputs.logits

            # Offset the logits by one to align with the labels
            labels = torch.roll(input_ids, shifts=-1, dims=1)
            loss_mask = torch.roll(loss_mask, shifts=-1, dims=1).bool()

            if self.use_logits_to_keep:
                # Align labels with logits
                # logits:    -,  -, [x2, x3, x4, x5, x6]
                #                     ^ --------- ^       after logits[:, :-1, :]
                # labels:   [y0, y1, y2, y3, y4, y5, y6]
                #                         ^ --------- ^   with logits_to_keep=4, [:, -4:]
                # loss_mask: [0,  0,  0,  1,  1,  1,  1]
                labels = labels[:, -logits_to_keep:]
                loss_mask = loss_mask[:, -logits_to_keep:]

        if logits.shape[:2] != labels.shape[:2]:
            # for llava, the returned logits include the image tokens (placed before the text tokens)
            seq_len = labels.shape[1]
            logits = logits[:, -seq_len:]

        # Compute the log probabilities of the labels
        labels[~loss_mask] = 0  # dummy token; we'll ignore the losses on these tokens later
        per_token_logps = selective_log_softmax(logits, labels)
        per_token_logps[~loss_mask] = 0
        per_token_logps = torch.roll(per_token_logps, shifts=1, dims=1)

        if self.padding_free:
            # Unflatten the per_token_logps (shape: [1, sum_seq_len] -> [batch_size, seq_len])
            batch_size, seq_len = attention_mask.shape
            per_token_logps_ = torch.zeros(
                batch_size, seq_len, device=outputs.logits.device, dtype=outputs.logits.dtype
            )
            per_token_logps_[attention_mask.bool()] = per_token_logps
            per_token_logps = per_token_logps_

        all_logps = per_token_logps[:, 1:].sum(-1)

        output = {}

        if self.use_weighting:
            with torch.no_grad():
                # Eq (2) of the WPO paper: https://huggingface.co/papers/2406.11827
                logprobs = F.log_softmax(logits, dim=-1)
                weights_adjustment_factor = torch.logsumexp(2 * logprobs, dim=-1)  # same as sum(probs**2) in log space
                per_token_logps_adjusted = per_token_logps - weights_adjustment_factor
                all_weights = (per_token_logps_adjusted * loss_mask).sum(-1) / loss_mask.sum(-1)
                # Split weights for 4 parts: [chosen_dpo, rejected_dpo, chosen_adpo, rejected_adpo]
                chosen_weights_dpo = all_weights[:num_examples]
                rejected_weights_dpo = all_weights[num_examples:2*num_examples]
                chosen_weights_adpo = all_weights[2*num_examples:3*num_examples]
                rejected_weights_adpo = all_weights[3*num_examples:4*num_examples]
                # Properly combine DPO and ADPO weights
                dpo_weights = torch.exp(chosen_weights_dpo + rejected_weights_dpo)
                adpo_weights = torch.exp(chosen_weights_adpo + rejected_weights_adpo)
                # Combine with lambda weighting (assuming lambda is available as self.lambda_weight)
                if hasattr(self, 'lambda_weight'):
                    combined_weights = self.lambda_weight * dpo_weights + (1 - self.lambda_weight) * adpo_weights
                else:
                    combined_weights = 0.5 * dpo_weights + 0.5 * adpo_weights  # default equal weighting
                output["policy_weights"] = torch.clamp(combined_weights, max=1)

        if self.args.rpo_alpha is not None:
            # Only use the chosen logits for the RPO loss
            chosen_logits = logits[:num_examples, :-1] if not self.is_encoder_decoder else logits[:num_examples]
            chosen_labels = labels[:num_examples, :-1] if not self.is_encoder_decoder else labels[:num_examples]

            # Compute the log probabilities of the labels
            output["nll_loss"] = F.cross_entropy(
                torch.flatten(chosen_logits, end_dim=1), torch.flatten(chosen_labels, end_dim=1), ignore_index=0
            )

        if self.loss_type == "ipo":
            all_logps = all_logps / loss_mask.sum(-1)

        if self.args.ld_alpha is not None and not is_ref_model:
            # Compute response lengths based on loss_mask
            completion_lengths = loss_mask.sum(dim=1)

            chosen_lengths = completion_lengths[:num_examples]
            rejected_lengths = completion_lengths[num_examples:]
            public_lengths = torch.min(chosen_lengths, rejected_lengths)  # l_p in the paper
            public_lengths = torch.cat([public_lengths, public_lengths], dim=0)

            seq_len = per_token_logps.size(1)
            position_ids = torch.arange(seq_len, device=per_token_logps.device).expand_as(per_token_logps)

            ld_mask = position_ids < public_lengths.unsqueeze(1)
            mask = position_ids < completion_lengths.unsqueeze(1)

            front_mask = (ld_mask & mask).float()
            rear_mask = (~ld_mask & mask).float()
            front_logps = (per_token_logps * front_mask).sum(dim=1)
            rear_logps = (per_token_logps * rear_mask).sum(dim=1)

            all_logps = front_logps + self.args.ld_alpha * rear_logps

        # Split the 4-part concatenated logps for DPO and ADPO losses:
        # Part 1: prompt + chosen_response → chosen_logps_dpo
        # Part 2: prompt + rejected_response → rejected_logps_dpo  
        # Part 3: chosen_prompt + response → chosen_logps_adpo
        # Part 4: rejected_prompt + response → rejected_logps_adpo
        output["chosen_logps_dpo"] = all_logps[:num_examples]
        output["rejected_logps_dpo"] = all_logps[num_examples:2*num_examples]
        output["chosen_logps_adpo"] = all_logps[2*num_examples:3*num_examples] 
        output["rejected_logps_adpo"] = all_logps[3*num_examples:4*num_examples]

        # Compute the mean logits for 4 parts: [chosen_dpo, rejected_dpo, chosen_adpo, rejected_adpo] 
        if self.padding_free:
            # position_ids contains a sequence of range identifiers (e.g., [[0, 1, 2, 0, 1, 2, 3, ...]]).
            # There are 4*num_examples ranges in total: [chosen_dpo, rejected_dpo, chosen_adpo, rejected_adpo]
            zero_positions = (position_ids == 0).nonzero(as_tuple=True)[1]
            split_idx_1 = zero_positions[num_examples]      # Start of rejected_dpo
            split_idx_2 = zero_positions[2*num_examples]    # Start of chosen_adpo  
            split_idx_3 = zero_positions[3*num_examples]    # Start of rejected_adpo
            
            mean_chosen_logits_dpo = logits[0, :split_idx_1][loss_mask[0, :split_idx_1]].mean()
            mean_rejected_logits_dpo = logits[0, split_idx_1:split_idx_2][loss_mask[0, split_idx_1:split_idx_2]].mean()
            mean_chosen_logits_adpo = logits[0, split_idx_2:split_idx_3][loss_mask[0, split_idx_2:split_idx_3]].mean()
            mean_rejected_logits_adpo = logits[0, split_idx_3:][loss_mask[0, split_idx_3:]].mean()
        else:
            mean_chosen_logits_dpo = logits[:num_examples][loss_mask[:num_examples]].mean()
            mean_rejected_logits_dpo = logits[num_examples:2*num_examples][loss_mask[num_examples:2*num_examples]].mean()
            mean_chosen_logits_adpo = logits[2*num_examples:3*num_examples][loss_mask[2*num_examples:3*num_examples]].mean()
            mean_rejected_logits_adpo = logits[3*num_examples:4*num_examples][loss_mask[3*num_examples:4*num_examples]].mean()

        # Store the 4 separate mean logits for DPO and ADPO
        output["mean_chosen_logits_dpo"] = mean_chosen_logits_dpo
        output["mean_rejected_logits_dpo"] = mean_rejected_logits_dpo
        output["mean_chosen_logits_adpo"] = mean_chosen_logits_adpo
        output["mean_rejected_logits_adpo"] = mean_rejected_logits_adpo

        if self.aux_loss_enabled:
            output["aux_loss"] = outputs.aux_loss

        return output

    def get_batch_loss_metrics(
        self,
        model,
        batch: dict[str, Union[list, torch.LongTensor]],
        train_eval: Literal["train", "eval"] = "train",
    ):
        """Compute the DPO loss and other metrics for the given batch of inputs for train or test."""
        metrics = {}

        if self.args.use_liger_loss:
            model_output = self._compute_loss_liger(model, batch)
            losses = model_output["loss"]
            chosen_rewards = model_output["chosen_rewards"]
            rejected_rewards = model_output["rejected_rewards"]
        else:
            model_output = self.concatenated_forward(model, batch)

            # if all 4 reference logps in batch use them, otherwise use the reference model
            if ("ref_chosen_logps_dpo" in batch and "ref_rejected_logps_dpo" in batch and 
                "ref_chosen_logps_adpo" in batch and "ref_rejected_logps_adpo" in batch):
                ref_chosen_logps_dpo = batch["ref_chosen_logps_dpo"]
                ref_rejected_logps_dpo = batch["ref_rejected_logps_dpo"]
                ref_chosen_logps_adpo = batch["ref_chosen_logps_adpo"]
                ref_rejected_logps_adpo = batch["ref_rejected_logps_adpo"]
            else:
                ref_chosen_logps_dpo, ref_rejected_logps_dpo, ref_chosen_logps_adpo, ref_rejected_logps_adpo = self.compute_ref_log_probs(batch)

            losses, chosen_rewards, rejected_rewards = self.multidpo_loss(
                model_output["chosen_logps_dpo"], model_output["rejected_logps_dpo"],
                model_output["chosen_logps_adpo"], model_output["rejected_logps_adpo"],
                ref_chosen_logps_dpo, ref_rejected_logps_dpo, ref_chosen_logps_adpo, ref_rejected_logps_adpo
            )
        reward_accuracies = (chosen_rewards > rejected_rewards).float()
        
        # Compute DPO and ADPO specific accuracies
        dpo_accuracies = (model_output["chosen_logps_dpo"] > model_output["rejected_logps_dpo"]).float()
        adpo_accuracies = (model_output["chosen_logps_adpo"] > model_output["rejected_logps_adpo"]).float()
        
        # Debug accuracy computation every 10 steps
        if hasattr(self, 'state') and self.state.global_step % 10 == 0:
            if hasattr(self, 'accelerator') and self.accelerator.is_main_process:
                print(f"\n🎯 Accuracy Debug (Step {self.state.global_step}):")
                print(f"  DPO: chosen_logps={model_output['chosen_logps_dpo'].mean().item():.2f}, rejected_logps={model_output['rejected_logps_dpo'].mean().item():.2f}")
                print(f"  DPO accuracy: {dpo_accuracies.mean().item():.3f}")
                print(f"  ADPO: chosen_logps={model_output['chosen_logps_adpo'].mean().item():.2f}, rejected_logps={model_output['rejected_logps_adpo'].mean().item():.2f}")
                print(f"  ADPO accuracy: {adpo_accuracies.mean().item():.3f}")
                
                # Show first few examples to understand the pattern
                if len(model_output['chosen_logps_adpo']) >= 3:
                    for i in range(min(3, len(model_output['chosen_logps_adpo']))):
                        chosen_adpo = model_output['chosen_logps_adpo'][i].item()
                        rejected_adpo = model_output['rejected_logps_adpo'][i].item()
                        correct = chosen_adpo > rejected_adpo
                        print(f"  Example {i}: chosen={chosen_adpo:.2f}, rejected={rejected_adpo:.2f}, correct={correct}")

        if self.args.rpo_alpha is not None:
            losses = losses + self.args.rpo_alpha * model_output["nll_loss"]  # RPO loss from V3 of the paper

        if self.use_weighting:
            losses = losses * model_output["policy_weights"]

        if self.aux_loss_enabled:
            losses = losses + self.aux_loss_coef * model_output["aux_loss"]

        prefix = "eval_" if train_eval == "eval" else ""
        metrics[f"{prefix}rewards/chosen"] = self.accelerator.gather_for_metrics(chosen_rewards).mean().item()
        metrics[f"{prefix}rewards/rejected"] = self.accelerator.gather_for_metrics(rejected_rewards).mean().item()
        metrics[f"{prefix}rewards/accuracies"] = self.accelerator.gather_for_metrics(reward_accuracies).mean().item()
        metrics[f"{prefix}dpo_accuracies"] = self.accelerator.gather_for_metrics(dpo_accuracies).mean().item()
        metrics[f"{prefix}adpo_accuracies"] = self.accelerator.gather_for_metrics(adpo_accuracies).mean().item()
        metrics[f"{prefix}rewards/margins"] = (
            self.accelerator.gather_for_metrics(chosen_rewards - rejected_rewards).mean().item()
        )
        metrics[f"{prefix}logps/chosen_dpo"] = (
            self.accelerator.gather_for_metrics(model_output["chosen_logps_dpo"]).detach().mean().item()
        )
        metrics[f"{prefix}logps/rejected_dpo"] = (
            self.accelerator.gather_for_metrics(model_output["rejected_logps_dpo"]).detach().mean().item()
        )
        metrics[f"{prefix}logps/chosen_adpo"] = (
            self.accelerator.gather_for_metrics(model_output["chosen_logps_adpo"]).detach().mean().item()
        )
        metrics[f"{prefix}logps/rejected_adpo"] = (
            self.accelerator.gather_for_metrics(model_output["rejected_logps_adpo"]).detach().mean().item()
        )
        metrics[f"{prefix}logits/chosen_dpo"] = (
            self.accelerator.gather_for_metrics(model_output["mean_chosen_logits_dpo"]).detach().mean().item()
        )
        metrics[f"{prefix}logits/rejected_dpo"] = (
            self.accelerator.gather_for_metrics(model_output["mean_rejected_logits_dpo"]).detach().mean().item()
        )
        metrics[f"{prefix}logits/chosen_adpo"] = (
            self.accelerator.gather_for_metrics(model_output["mean_chosen_logits_adpo"]).detach().mean().item()
        )
        metrics[f"{prefix}logits/rejected_adpo"] = (
            self.accelerator.gather_for_metrics(model_output["mean_rejected_logits_adpo"]).detach().mean().item()
        )
        if self.args.rpo_alpha is not None:
            metrics[f"{prefix}nll_loss"] = (
                self.accelerator.gather_for_metrics(model_output["nll_loss"]).detach().mean().item()
            )
        if self.aux_loss_enabled:
            metrics[f"{prefix}aux_loss"] = (
                self.accelerator.gather_for_metrics(model_output["aux_loss"]).detach().mean().item()
            )

        return losses.mean(), metrics

    def compute_loss(
        self,
        model: Union[PreTrainedModel, nn.Module],
        inputs: dict[str, Union[torch.Tensor, Any]],
        return_outputs=False,
        num_items_in_batch=None,
    ) -> Union[torch.Tensor, tuple[torch.Tensor, dict[str, torch.Tensor]]]:
        compute_loss_context_manager = (
            autocast(self.accelerator.device.type) if self._peft_has_been_casted_to_bf16 else nullcontext()
        )
        with compute_loss_context_manager:
            loss, metrics = self.get_batch_loss_metrics(model, inputs, train_eval="train")

        # Make sure to move the loss to the device the original accumulating loss is at back in the `Trainer` class:
        loss = loss.to(self.args.device)
        # force log the metrics
        self.store_metrics(metrics, train_eval="train")

        if return_outputs:
            return loss, metrics

        return loss

    def generate_from_model_and_ref(self, model, batch: dict[str, torch.LongTensor]) -> tuple[str, str]:
        """Generate samples from the model and reference model for the given batch of inputs."""

        # If one uses `generate_during_eval` with peft + bf16, we need to explicitly call generate with
        # the torch amp context manager as some hidden states are silently casted to full precision.
        generate_context_manager = (
            autocast(self.accelerator.device.type) if self._peft_has_been_casted_to_bf16 else nullcontext()
        )

        with generate_context_manager:
            policy_output = model.generate(
                input_ids=batch["prompt_input_ids"],
                attention_mask=batch["prompt_attention_mask"],
                max_length=self.max_length,
                do_sample=True,
                pad_token_id=self.padding_value,
            )

            # if ref_output in batch use that otherwise use the reference model
            if "ref_output" in batch:
                ref_output = batch["ref_output"]
            else:
                if self.ref_model is None:
                    with self.null_ref_context():
                        ref_output = self.model.generate(
                            input_ids=batch["prompt_input_ids"],
                            attention_mask=batch["prompt_attention_mask"],
                            max_length=self.max_length,
                            do_sample=True,
                            pad_token_id=self.padding_value,
                        )
                else:
                    ref_output = self.ref_model.generate(
                        input_ids=batch["prompt_input_ids"],
                        attention_mask=batch["prompt_attention_mask"],
                        max_length=self.max_length,
                        do_sample=True,
                        pad_token_id=self.padding_value,
                    )

        policy_output = pad_to_length(policy_output, self.max_length, self.padding_value)
        policy_output_decoded = self.processing_class.batch_decode(policy_output, skip_special_tokens=True)

        ref_output = pad_to_length(ref_output, self.max_length, self.padding_value)
        ref_output_decoded = self.processing_class.batch_decode(ref_output, skip_special_tokens=True)

        return policy_output_decoded, ref_output_decoded

    def prediction_step(
        self,
        model: Union[PreTrainedModel, nn.Module],
        inputs: dict[str, Union[torch.Tensor, Any]],
        prediction_loss_only: bool,
        ignore_keys: Optional[list[str]] = None,
    ):
        if ignore_keys is None:
            if hasattr(model, "config"):
                ignore_keys = getattr(model.config, "keys_to_ignore_at_inference", [])
            else:
                ignore_keys = []

        prediction_context_manager = (
            autocast(self.accelerator.device.type) if self._peft_has_been_casted_to_bf16 else nullcontext()
        )

        with torch.no_grad(), prediction_context_manager:
            loss, metrics = self.get_batch_loss_metrics(model, inputs, train_eval="eval")

        # force log the metrics
        self.store_metrics(metrics, train_eval="eval")

        if prediction_loss_only:
            return loss.detach(), None, None

        # logits for the chosen and rejected samples from model (4-part MultiDPO structure)
        logits_dict = {
            "eval_logits/chosen_dpo": metrics["eval_logits/chosen_dpo"],
            "eval_logits/rejected_dpo": metrics["eval_logits/rejected_dpo"],
            "eval_logits/chosen_adpo": metrics["eval_logits/chosen_adpo"],
            "eval_logits/rejected_adpo": metrics["eval_logits/rejected_adpo"],
        }
        logits = [v for k, v in logits_dict.items() if k not in ignore_keys]
        logits = torch.tensor(logits, device=self.accelerator.device)
        labels = torch.zeros(logits.shape[0], device=self.accelerator.device)

        return (loss.detach(), logits, labels)

    def store_metrics(self, metrics: dict[str, float], train_eval: Literal["train", "eval"] = "train") -> None:
        for key, value in metrics.items():
            self._stored_metrics[train_eval][key].append(value)

    def evaluation_loop(
        self,
        dataloader: DataLoader,
        description: str,
        prediction_loss_only: Optional[bool] = None,
        ignore_keys: Optional[list[str]] = None,
        metric_key_prefix: str = "eval",
    ) -> EvalLoopOutput:
        """
        Overriding built-in evaluation loop to store metrics for each batch. Prediction/evaluation loop, shared by
        `Trainer.evaluate()` and `Trainer.predict()`.

        Works both with or without labels.
        """

        # Sample and save to game log if requested (for one batch to save time)
        if self.generate_during_eval:
            # Generate random indices within the range of the total number of samples
            num_samples = len(dataloader.dataset)
            random_indices = random.sample(range(num_samples), k=self.args.eval_batch_size)

            # Use dataloader.dataset.select to get the random batch without iterating over the DataLoader
            random_batch_dataset = dataloader.dataset.select(random_indices)
            random_batch = self.data_collator(random_batch_dataset)
            random_batch = self._prepare_inputs(random_batch)

            policy_output_decoded, ref_output_decoded = self.generate_from_model_and_ref(self.model, random_batch)

            table = pd.DataFrame(
                columns=["Prompt", "Policy", "Ref Model"],
                data=[
                    [prompt, pol[len(prompt) :], ref[len(prompt) :]]
                    for prompt, pol, ref in zip(
                        random_batch_dataset["prompt"], policy_output_decoded, ref_output_decoded
                    )
                ],
            )
            if "wandb" in self.args.report_to and self.accelerator.is_main_process:
                wandb.log({"game_log": wandb.Table(data=table)})

            if "comet_ml" in self.args.report_to:
                log_table_to_comet_experiment(
                    name="game_log.csv",
                    table=table,
                )

        # Base evaluation
        initial_output = super().evaluation_loop(
            dataloader, description, prediction_loss_only, ignore_keys, metric_key_prefix
        )

        return initial_output

    def log(self, logs: dict[str, float], start_time: Optional[float] = None) -> None:
        """
        Log `logs` on the various objects watching training, including stored metrics.

        Args:
            logs (`dict[str, float]`):
                The values to log.
            start_time (`float` or `None`, *optional*, defaults to `None`):
                Start time of the training.
        """
        # logs either has 'loss' or 'eval_loss'
        train_eval = "train" if "loss" in logs else "eval"
        # Add averaged stored metrics to logs
        for key, metrics in self._stored_metrics[train_eval].items():
            logs[key] = torch.tensor(metrics).mean().item()
        del self._stored_metrics[train_eval]
        return super().log(logs, start_time)

    # Ensure the model card is saved along with the checkpoint
    def _save_checkpoint(self, model, trial):
        """Enhanced checkpoint saving with debugging and verification."""
        if self.args.hub_model_id is None:
            model_name = Path(self.args.output_dir).name
        else:
            model_name = self.args.hub_model_id.split("/")[-1]
        
        # Debug: Print model state before saving
        self._debug_model_state(model, "before_save")
        
        self.create_model_card(model_name=model_name)
        
        # Call parent checkpoint saving
        super()._save_checkpoint(model, trial)
        
        # Verify checkpoint was saved correctly
        if hasattr(self, 'state') and self.state.global_step > 0:
            checkpoint_dir = f"{self.args.output_dir}/checkpoint-{self.state.global_step}"
            self._verify_checkpoint_saved(checkpoint_dir, model)

    def _debug_model_state(self, model, prefix=""):
        """Debug helper to print model parameter statistics."""
        if self.accelerator.is_main_process:
            param_count = sum(p.numel() for p in model.parameters())
            trainable_count = sum(p.numel() for p in model.parameters() if p.requires_grad)
            
            # Calculate parameter norms
            total_norm = 0.0
            grad_norm = 0.0
            param_stats = []
            
            for name, param in model.named_parameters():
                if param.requires_grad:
                    param_norm = param.data.norm().item()
                    total_norm += param_norm ** 2
                    
                    if param.grad is not None:
                        grad_norm += param.grad.data.norm().item() ** 2
                    
                    param_stats.append((name, param_norm, param.grad is not None))
            
            total_norm = total_norm ** 0.5
            grad_norm = grad_norm ** 0.5
            
            print(f"\n=== Model State Debug ({prefix}) ===")
            print(f"Total parameters: {param_count:,}")
            print(f"Trainable parameters: {trainable_count:,}")
            print(f"Parameter norm: {total_norm:.6f}")
            print(f"Gradient norm: {grad_norm:.6f}")
            print(f"Lambda weight: {getattr(self, 'lambda_weight', 'NOT_SET')}")
            print(f"Model mode: {'training' if model.training else 'eval'}")
            
            # Show first few parameter stats
            print("First 3 trainable parameters:")
            for name, norm, has_grad in param_stats[:3]:
                print(f"  {name}: norm={norm:.6f}, has_grad={has_grad}")
            print("=" * 50)

    def _verify_checkpoint_saved(self, checkpoint_dir, model):
        """Verify that checkpoint was actually saved and contains updated parameters."""
        if not os.path.exists(checkpoint_dir):
            print(f"WARNING: Checkpoint directory {checkpoint_dir} does not exist!")
            return
            
        # Check if model files exist
        model_files = ["pytorch_model.bin", "model.safetensors", "adapter_model.bin"]
        found_model_file = None
        
        for model_file in model_files:
            model_path = os.path.join(checkpoint_dir, model_file)
            if os.path.exists(model_path):
                found_model_file = model_path
                break
        
        if found_model_file is None:
            print(f"WARNING: No model file found in {checkpoint_dir}")
            return
            
        print(f"✓ Checkpoint saved at {checkpoint_dir}")
        print(f"✓ Model file: {os.path.basename(found_model_file)}")
        
        # Additional verification for DeepSpeed
        if self.is_deepspeed_enabled:
            ds_config_path = os.path.join(checkpoint_dir, "zero_to_fp32.py")
            if os.path.exists(ds_config_path):
                print("✓ DeepSpeed checkpoint files detected")
            else:
                print("WARNING: DeepSpeed checkpoint files may be missing")

    def save_model_state_dict(self, output_dir: str = None):
        """Explicit model state dict saving as backup."""
        if output_dir is None:
            output_dir = self.args.output_dir
            
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Get the unwrapped model for saving
        model_to_save = self.accelerator.unwrap_model(self.model)
        
        # Save model state dict
        model_path = os.path.join(output_dir, "multidpo_model_state.pt")
        
        if self.accelerator.is_main_process:
            state_dict = model_to_save.state_dict()
            torch.save({
                'model_state_dict': state_dict,
                'training_args': self.args,
                'lambda_weight': self.lambda_weight,
                'global_step': self.state.global_step if hasattr(self, 'state') else 0,
            }, model_path)
            print(f"✓ Explicit model state dict saved to {model_path}")
        
        return model_path

    def training_step(self, model, inputs, num_items_in_batch=None):
        """Override training step to monitor parameter updates."""
        # Store parameter norms before training step
        if hasattr(self, '_param_norms_before'):
            param_norms_before = self._param_norms_before
        else:
            param_norms_before = {}
            for name, param in model.named_parameters():
                if param.requires_grad:
                    param_norms_before[name] = param.data.norm().item()
        
        # Perform the actual training step
        loss = super().training_step(model, inputs)
        
        # Check parameter updates and gradients after training step
        if self.state.global_step % self.args.logging_steps == 0:
            param_updates = []
            grad_info = []
            
            for name, param in model.named_parameters():
                if param.requires_grad:
                    # Parameter update info
                    if name in param_norms_before:
                        norm_before = param_norms_before[name]
                        norm_after = param.data.norm().item()
                        update_magnitude = abs(norm_after - norm_before)
                        param_updates.append((name, norm_before, norm_after, update_magnitude))
                    
                    # Gradient info
                    if param.grad is not None:
                        grad_norm = param.grad.data.norm().item()
                        grad_info.append((name, grad_norm))
                    else:
                        grad_info.append((name, 0.0))
            
            # Log comprehensive debugging info
            if self.accelerator.is_main_process:
                # Calculate meaningful parameter update metrics
                avg_update = sum(update[3] for update in param_updates) / len(param_updates) if param_updates else 0
                total_grad_norm = sum(grad[1] for grad in grad_info)
                max_update = max(param_updates, key=lambda x: x[3]) if param_updates else None
                max_grad = max(grad_info, key=lambda x: x[1]) if grad_info else None
                
                # Calculate parameter update norm (RMS of all updates)
                param_update_norm = (sum(update[3]**2 for update in param_updates) / len(param_updates))**0.5 if param_updates else 0
                
                print(f"\n🔍 Step {self.state.global_step} Debug Info:")
                print(f"  📊 Loss: {loss.item():.6f}")
                print(f"  📈 Avg parameter change: {avg_update:.8f}")
                print(f"  📐 Parameter update norm (RMS): {param_update_norm:.8f}")
                print(f"  📉 Total gradient norm: {total_grad_norm:.8f}")
                if max_update:
                    print(f"  🎯 Max param change: {max_update[0][:50]}... = {max_update[3]:.8f}")
                if max_grad:
                    print(f"  ⚡ Max gradient: {max_grad[0][:50]}... = {max_grad[1]:.8f}")
                
                # Enhanced warnings
                if avg_update < 1e-8:
                    print("⚠️  WARNING: Very small parameter updates detected!")
                    if total_grad_norm < 1e-8:
                        print("⚠️  CRITICAL: No gradients detected! Model not learning!")
                    else:
                        print("⚠️  ISSUE: Gradients present but parameters not updating!")
                
                # Check learning rate
                current_lr = self.optimizer.param_groups[0]['lr'] if self.optimizer else 'Unknown'
                print(f"  🎓 Current learning rate: {current_lr}")
                
                # Check DeepSpeed stage
                if hasattr(self, 'accelerator') and hasattr(self.accelerator.state, 'deepspeed_plugin'):
                    if self.accelerator.state.deepspeed_plugin:
                        zero_stage = getattr(self.accelerator.state.deepspeed_plugin, 'zero_stage', 'Unknown')
                        print(f"  🚀 DeepSpeed ZeRO Stage: {zero_stage}")
        
        # Store current norms for next step
        self._param_norms_before = {}
        for name, param in model.named_parameters():
            if param.requires_grad:
                self._param_norms_before[name] = param.data.norm().item()
        
        return loss

    def create_model_card(
        self,
        model_name: Optional[str] = None,
        dataset_name: Optional[str] = None,
        tags: Union[str, list[str], None] = None,
    ):
        """
        Creates a draft of a model card using the information available to the `Trainer`.

        Args:
            model_name (`str` or `None`, *optional*, defaults to `None`):
                Name of the model.
            dataset_name (`str` or `None`, *optional*, defaults to `None`):
                Name of the dataset used for training.
            tags (`str`, `list[str]` or `None`, *optional*, defaults to `None`):
                Tags to be associated with the model card.
        """
        if not self.is_world_process_zero():
            return

        if hasattr(self.model.config, "_name_or_path") and not os.path.isdir(self.model.config._name_or_path):
            base_model = self.model.config._name_or_path
        else:
            base_model = None

        # normalize `tags` to a mutable set
        if tags is None:
            tags = set()
        elif isinstance(tags, str):
            tags = {tags}
        else:
            tags = set(tags)

        if hasattr(self.model.config, "unsloth_version"):
            tags.add("unsloth")

        tags.update(self._tag_names)

        citation = textwrap.dedent(
            """\
            @inproceedings{rafailov2023direct,
                title        = {{Direct Preference Optimization: Your Language Model is Secretly a Reward Model}},
                author       = {Rafael Rafailov and Archit Sharma and Eric Mitchell and Christopher D. Manning and Stefano Ermon and Chelsea Finn},
                year         = 2023,
                booktitle    = {Advances in Neural Information Processing Systems 36: Annual Conference on Neural Information Processing Systems 2023, NeurIPS 2023, New Orleans, LA, USA, December 10 - 16, 2023},
                url          = {http://papers.nips.cc/paper_files/paper/2023/hash/a85b405ed65c6477a4fe8302b5e06ce7-Abstract-Conference.html},
                editor       = {Alice Oh and Tristan Naumann and Amir Globerson and Kate Saenko and Moritz Hardt and Sergey Levine},
            }"""
        )

        model_card = generate_model_card(
            base_model=base_model,
            model_name=model_name,
            hub_model_id=self.hub_model_id,
            dataset_name=dataset_name,
            tags=tags,
            wandb_url=wandb.run.get_url() if is_wandb_available() and wandb.run is not None else None,
            comet_url=get_comet_experiment_url(),
            trainer_name="DPO",
            trainer_citation=citation,
            paper_title="Direct Preference Optimization: Your Language Model is Secretly a Reward Model",
            paper_id="2305.18290",
        )

        model_card.save(os.path.join(self.args.output_dir, "README.md"))
