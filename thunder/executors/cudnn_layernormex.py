from typing import Any

import torch
import numpy as np


# WARNING: cudnn layernorm executor is experimental. Tests that use cudnn might fail.
from dataclasses import dataclass
from functools import lru_cache


from thunder.executors.cudnnex import cudnn_available, torch_to_cudnn_dtype
from thunder.extend import OperatorExecutor


@dataclass(frozen=True)
class CudnnTensorAttributes:
    size: tuple[int, ...]
    stride: tuple[int, ...]
    dtype: torch.dtype
    device_index: int


def make_cacheable_cudnn_graph_inputs(func):
    def wrapper(*args, **kwargs):
        cudnn_input_args = [
            (
                CudnnTensorAttributes(arg.size(), arg.stride(), arg.dtype, args.device_index)
                if isinstance(arg, torch.Tensor)
                else arg
            )
            for arg in args
        ]
        return func(*cudnn_input_args, **kwargs)

    return wrapper


@make_cacheable_cudnn_graph_inputs
@lru_cache(maxsize=1024)
def _make_cudnn_layer_norm_graph(a_4d, weight_4d, bias_4d):
    graph = cudnn.pygraph(intermediate_data_type=cudnn.data_type.FLOAT, compute_data_type=cudnn.data_type.FLOAT)

    input = graph.tensor(name="input", dim=a_4d.size, stride=a_4d.stride, data_type=torch_to_cudnn_dtype(a_4d.dtype))
    scale = graph.tensor(
        name="scale", dim=weight_4d.size, stride=weight_4d.stride, data_type=torch_to_cudnn_dtype(weight_4d.dtype)
    )
    bias = graph.tensor(
        name="bias", dim=bias_4d.size, stride=bias_4d.stride, data_type=torch_to_cudnn_dtype(bias_4d.dtype)
    )

    epsilon = graph.tensor(
        name="epsilon", dim=[1, 1, 1, 1], stride=[1, 1, 1, 1], data_type=cudnn.data_type.FLOAT, is_pass_by_value=True
    )

    Y, _, _ = graph.layernorm(
        name="LN",
        norm_forward_phase=cudnn.norm_forward_phase.INFERENCE,
        input=input,
        scale=scale,
        bias=bias,
        epsilon=epsilon,
    )

    Y.set_output(True).set_data_type(torch_to_cudnn_dtype(a_4d.dtype)).set_stride(a_4d.stride)

    graph.build([cudnn.heur_mode.A])

    return input, scale, bias, epsilon, Y, graph


# cudnn only supports following:
# input tensor shape: N, C, (D), H, W
# normalized shape:  (C, (D), H, W)
# convert all tensor shapes to above format
def _transform_layer_norm_inputs(a, normalized_shape, weight, bias):
    elements_to_normalize = np.prod(normalized_shape)
    batch_size = np.prod(a.shape[: -len(normalized_shape)], dtype=int)

    # Assume strides to be NCHW contiguous
    assumed_stride = (elements_to_normalize, 1, 1, 1)
    a_4d = CudnnTensorAttributes((batch_size, elements_to_normalize, 1, 1), assumed_stride, a.dtype, a.device.index)
    weight_4d = CudnnTensorAttributes(
        (1, elements_to_normalize, 1, 1), assumed_stride, weight.dtype, weight.device.index
    )
    bias_4d = CudnnTensorAttributes((1, elements_to_normalize, 1, 1), assumed_stride, bias.dtype, bias.device.index)

    return a_4d, weight_4d, bias_4d


def layer_norm_impl(a, normalized_shape, weight=None, bias=None, eps=1e-5):
    a_4d, weight_4d, bias_4d = _transform_layer_norm_inputs(a, normalized_shape, weight, bias)
    input, scale, B, epsilon, Y, graph = _make_cudnn_layer_norm_graph(a_4d, weight_4d, bias_4d)

    Y_actual = torch.zeros_like(a, device="cuda")

    epsilon_cpu = torch.full((1, 1, 1, 1), eps, dtype=torch.float32, device="cpu")

    workspace = torch.empty(graph.get_workspace_size(), device="cuda", dtype=torch.uint8)

    cudnn_to_torch_tensor = {input: a, scale: weight, B: bias, epsilon: epsilon_cpu, Y: Y_actual}

    graph.execute(cudnn_to_torch_tensor, workspace)

    return Y_actual


def layer_norm_checker(a, normalized_shape, weight=None, bias=None, eps=1e-5):
    if cudnn is None:
        return False

    a_4d, weight_4d, bias_4d = _transform_layer_norm_inputs(a, normalized_shape, weight, bias)

    try:
        _make_cudnn_layer_norm_graph(a_4d, weight_4d, bias_4d)
    except:
        return False

    return True


cudnn_layernorm_ex: None | OperatorExecutor = None

if cudnn_available():
    from thunder.extend import register_executor
    import cudnn

    cudnn_layernorm_ex: OperatorExecutor = OperatorExecutor("cudnn_layernorm", version=cudnn.backend_version())
    register_executor(cudnn_layernorm_ex)

    import thunder.torch as ltorch

    layer_norm = cudnn_layernorm_ex.register_operator("cudnn_layernorm", like=ltorch.layer_norm, fn=layer_norm_impl)
    cudnn_layernorm_ex.register_implementation(ltorch.layer_norm, layer_norm, checker=layer_norm_checker)
