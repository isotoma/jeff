"""Force transformers' Qwen3.5 onto its pure-PyTorch reference path on CPU boxes.

Qwen3.5's gated-delta-rule attention ships with two implementations: a pure-torch
reference (`torch_chunk_gated_delta_rule` in transformers) and a faster one in
`flash-linear-attention` (fla). transformers' `use_kernel_func_from_hub_with_fallback`
decorator prefers fla whenever it imports successfully — and fla's code paths call
`@triton.jit` kernels that crash with `RuntimeError: 0 active drivers` on a machine
with no CUDA driver.

Rather than chase every Triton kernel fla calls, we block the `fla` import entirely
when CUDA is unavailable. The decorator's `try: import_module("fla") except
ImportError: implementation = torch_function` branch then falls back to the
pure-torch reference, which is correct (just slower) and needs no GPU.

This must run before `transformers.models.qwen3_5.modeling_qwen3_5` is imported
(it happens at decoration time, not call time), so `jeff.__init__` imports it.
On CUDA boxes we do nothing: fla's kernels are faster and stay in use.
"""

from __future__ import annotations

import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec


class _FlaBlocker(MetaPathFinder):
    """Raise ImportError for `fla` (and any submodule) so transformers falls back to torch."""

    def find_spec(self, name: str, path: object, target: object = None) -> ModuleSpec | None:
        if name == "fla" or name.startswith("fla."):
            raise ImportError(f"{name!r} is blocked on CPU; using transformers' pure-torch fallback")
        return None


def apply_cpu_fallback() -> None:
    if sys.modules.get("fla") is not None:
        return
    import torch

    if torch.cuda.is_available():
        return
    if not any(isinstance(finder, _FlaBlocker) for finder in sys.meta_path):
        sys.meta_path.insert(0, _FlaBlocker())
