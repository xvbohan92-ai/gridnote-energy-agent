"""Identical unconstrained greedy generation for base and LoRA SQL planners."""

import time

from mlx_lm import stream_generate
from mlx_lm.sample_utils import make_sampler


def generate_plan(model, tokenizer, messages, config):
    tokens = tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, enable_thinking=False, tokenize=True, return_dict=False
    )
    started = time.perf_counter()
    text, final = "", None
    for chunk in stream_generate(
        model,
        tokenizer,
        tokens,
        max_tokens=config["generation_max_tokens"],
        sampler=make_sampler(temp=config["generation_temperature"]),
    ):
        text += chunk.text
        final = chunk
    return text, {
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "input_tokens": final.prompt_tokens if final else len(tokens),
        "output_tokens": final.generation_tokens if final else 0,
        "finish_reason": final.finish_reason if final else "no_output",
    }
