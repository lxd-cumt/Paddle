# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
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

import logging
import typing

import paddle
from paddle import pir
from paddle.autograd import ir_backward
from paddle.base.core import (
    call_decomp,
    decomp_ops_contain_unused_output,
    has_decomp,
)
from paddle.base.libpaddle.pir import Block, Operation, Program
from paddle.framework import core

from . import register


def _build_tensor_tuple(xs):
    if isinstance(xs, pir.OpResult):
        return (xs,)
    elif isinstance(xs, typing.Sequence):
        return tuple(xs)
    return TypeError(f"Type {type(xs)} is not supported.")


def _analyse_decomp_results(orig_outs, decomp_outs, op):
    assert len(orig_outs) == len(decomp_outs)
    res = []
    for idx, value in enumerate(decomp_outs):
        if isinstance(orig_outs[idx], pir.OpResult):
            if (
                op.name() in decomp_ops_contain_unused_output.keys()
                and idx in decomp_ops_contain_unused_output[op.name()]
            ):
                assert value[0] is None
            else:
                assert len(value) == 1 and isinstance(value[0], pir.OpResult)
            res.append(value[0])
        else:
            res.append(value)
    return res


def _prepare_python_api_arguments(op):
    """
    For standard api of operator, its inputs should keep consistent with organization of its inputs and attrs.

    Args:
    op (Operator): The target operator.
    """
    combine_op_name = "builtin.combine"
    inputs = []
    for x in op.operands():
        input = x.source()
        if input and input.initialized():
            prev_op = input.get_defining_op()
            if (
                isinstance(prev_op, Operation)
                and prev_op.name() == combine_op_name
            ):
                input = [item.source() for item in prev_op.operands()]
            inputs.append(input)
        else:
            # for optional input, such as scale for layer_norm op,
            # if it is not set, there will be an empty OpResult which is not initialized in ops.operands
            # therefore append None for it.
            inputs.append(None)

    # The inputs of Pir op builtin.combine will be restored as list of tensor.
    if op.name() == combine_op_name:
        return (inputs,)

    api_arguments = inputs + [op.attrs()[x] for x in op.get_attr_names()]
    return tuple(api_arguments)


def _check_op_results(
    op_name, orig_outs, new_outs, orig_vars=None, dst_vars=None
):
    """
    Check whether the replaced outputs are consistent with origin outputs.

    Args:
    op_name (str): The name of operator.
    orig_outs (tuple): The outputs of original operator.
    new_outs (tuple): The outputs of replaced operator.
    orig_vars (dict): Origin variables of original block.
    dst_vars (list): Corresponding replaced variables of Origin variables.
    """
    assert len(orig_outs) == len(new_outs), (
        f'when replace origin op {op_name} with composite rule, num of origin outs should be equal to new outs, '
        f'but len(orig_outs) = {len(orig_outs)} and len(new_outs) = {len(new_outs)}'
    )

    for orig_out, new_out in zip(
        orig_outs,
        new_outs,
    ):
        if (orig_out is None or new_out is None) and (
            op_name not in core.ops_contain_none
        ):
            raise ValueError(
                f"op {op_name} should not contain any None value. original outs={orig_outs} and its composite rule outs={new_outs}"
            )
        if orig_out is None:
            # to keep same as phi op definition, orig_out may receive None
            continue
        elif new_out is not None:
            if orig_vars is not None and dst_vars is not None:
                if orig_out in orig_vars.keys():
                    dst_vars[orig_vars[orig_out]] = new_out
            orig_dtype = orig_out.dtype
            new_dtype = new_out.dtype
            orig_shape = orig_out.shape
            new_shape = new_out.shape
            assert orig_dtype == new_dtype, (
                f'when replace origin op {op_name} with composite rule, origin out dtype should be equal to new out dtype, '
                f'but orig_out dtype={orig_dtype} and new_out dtype={new_dtype}'
            )
            assert (
                -1 not in new_shape
            ), f'when replace origin op {op_name} with composite rule, composite out shape has -1.'
            assert orig_shape == new_shape, (
                f'when replace origin op {op_name} with composite rule, origin out shape should be equal to new out shape, '
                f'but orig_out shape={orig_shape} and new_out shape={new_shape}'
            )
            assert not (orig_out is None) ^ (
                new_out is None
            ), "orig_out and new_out should match."
        return


def decompose(
    program,
    src_vars,
    blacklist=frozenset(),
    whitelist=frozenset(),
):
    """
    Search nonbasic ops which have be registered composite rules and replace them with primitive ops.
    The operators in blacklist will be excluded from program when decomposed into primitives, and only the
    operators in whitelist will be decomposed. The priority of blacklist is higher than whitelist, it means
    an operator both in blacklist and whitelist will not be decomposed.

    The finally set that will be decomposed is:
        (block.ops & ops have decomposite rule & whitelist) - blacklist

    Note:
        All variables must be contained inside the given program.

    Args:
        program (Program): The program to be processed.
        src_vars (list[OpResult]): In program, once some operator is decomposed, its vars will be replaced by new ones. This argument means some vars will be used later and corresponding vars will be returned for later usage.
        blacklist (frozenset): The Operators that will be exclude when decomposed into primitives.
        whitelist (frozenset): Only the operators in whitelist will be decomposed into primitives.

    Returns:
        dst_vars (list): A list contains all vars which replace origin ones in src_vars.
    """
    if not core._is_fwd_prim_enabled():
        return src_vars
    if not isinstance(program, Program):
        raise TypeError(f"Expect type Program, but got type {type(program)}.")
    block = program.global_block()

    if not isinstance(blacklist, (set, frozenset)):
        raise TypeError(
            f'Expected type of blacklisst is set|frozenset, but got {type(blacklist)}.'
        )
    if not isinstance(whitelist, (set, frozenset)):
        raise TypeError(
            f'Expected type of whiltelist is set|frozenset, but got {type(whitelist)}.'
        )

    blacklist = core.prim_config["forward_blacklist"] | blacklist

    logging.debug("Decompose composite forward ops begin...")

    if len(blacklist) > 0 and len(whitelist) > 0:
        op_filter = (
            lambda x: x.name() in whitelist and x.name() not in blacklist
        )
    elif len(blacklist) > 0 and len(whitelist) == 0:
        op_filter = lambda x: x.name() not in blacklist
    elif len(blacklist) == 0 and len(whitelist) > 0:
        op_filter = lambda x: x.name() in whitelist
    else:
        op_filter = lambda x: True
    dst_vars = [None] * len(src_vars)
    dst_vars_dct = {}
    for idx, item in enumerate(src_vars):
        if not isinstance(item, pir.OpResult):
            raise TypeError(
                f"Each var in dst_vars should map corresponding var in src_vars, but got type {type(item)} in {src_vars}."
            )
        dst_vars_dct[item] = idx
    with pir.core.program_guard(program):
        _decompose_subgraph(
            block,
            dst_vars_dct,
            dst_vars,
            op_filter,
        )
    for idx, item in enumerate(dst_vars):
        if not isinstance(item, pir.OpResult):
            if item is None:
                dst_vars[idx] = src_vars[idx]
            else:
                raise TypeError(
                    f"Each var in dst_vars should map corresponding var in src_vars, but got type {type(item)} in {dst_vars}."
                )
    logging.debug(
        "Decompose composite forward ops finish: {}".format(
            core.prim_config["composite_ops_record"]
        )
    )
    return dst_vars


def _decompose_subgraph(block, orig_vars, dst_vars, op_filter):
    """
    The operators in block wich satisfy the filter conditon will be decomposed into primitives.

    Args:
        block (Block|Sequence[Block]): The blocks of program to be processed.
        op_filter (function): The filter to specify which ops to be processed.
        orig_vars (dict): Origin variables of original block.
        dst_vars (list): Corresponding replaced variables of Origin variables.
    """

    if isinstance(block, Block):
        ops_list = block.ops
        temp_op = None
        for idx, op in enumerate(ops_list):
            op_name = op.name()
            decom_rule = register.get_decomp_rule(op_name)
            has_sink_decomp_rule = has_decomp(op)
            lower = (decom_rule or has_sink_decomp_rule) and op_filter(op)

            if op.name() == "builtin.combine":
                temp_op = op

            if lower:
                core.prim_config["composite_ops_record"].add(op_name)
                if (
                    temp_op is not None
                    and ops_list[idx - 1].name() == "builtin.combine"
                ):
                    pir.set_insertion_point(temp_op)
                else:
                    pir.set_insertion_point(op)
                input_args = _prepare_python_api_arguments(op)
                orig_outs = op.results()
                if has_sink_decomp_rule:
                    decomp_outs = call_decomp(op)
                    new_outs = _analyse_decomp_results(
                        orig_outs, decomp_outs, op
                    )
                else:
                    new_outs = _build_tensor_tuple(decom_rule(*input_args))

                # Todo: To cover such case: some outputs are no longer needed after decomposition.
                _check_op_results(
                    op_name, orig_outs, new_outs, orig_vars, dst_vars
                )
                if op.name() in decomp_ops_contain_unused_output.keys():
                    for idx in range(len(orig_outs)):
                        if (
                            idx
                            not in decomp_ops_contain_unused_output[op.name()]
                        ):
                            orig_outs[idx].replace_all_uses_with(new_outs[idx])
                else:
                    if op.name() in decomp_ops_contain_unused_output.keys():
                        orig_outs[0].replace_all_uses_with(new_outs[0])
                    else:
                        op.replace_all_uses_with(new_outs)
                block.remove_op(op)

                if temp_op is not None:
                    remove_op = True
                    for item in temp_op.results():
                        if item.has_one_use():
                            remove_op = False
                            break
                    if remove_op:
                        block.remove_op(temp_op)
                    temp_op = None
        return

    elif isinstance(block, typing.Sequence):
        for item in block:
            _decompose_subgraph(item, orig_vars, dst_vars, op_filter)
        return
    raise TypeError(
        f"Expect type Block or Sequence of Block, but got type {type(block)}"
    )


def _decomp_fwd_op(
    block: Block, fwd_op: pir.Operation, grad_var_to_var: dict
) -> tuple:
    '''
    Decompose the forward op into a list of primitive ops.

    Args:
        block (Block): the block to which the forward op belongs.
        fwd_op (pir.Operation): the forward op to be decomposed.
        grad_var_to_var (dict): a dict obtained from distributed processing,
            which maps the backward grad variable to its corresponding forward variable.
    Returns:
        new_outputs (tuple(Value)): the new outputs after decomposing.
        has_decomposed: whether the forward op has been successfully decomposed.
    '''

    if not core._is_fwd_prim_enabled():
        raise RuntimeError(
            "To decompose forward op, please set `core._set_prim_forward_enabled(True)` firstly"
        )

    with pir.core.program_guard(block.program):
        op_name = fwd_op.name()
        orig_outs = fwd_op.results()
        decom_rule = register.get_decomp_rule(op_name)
        has_sink_decomp_rule = has_decomp(fwd_op)
        lower = decom_rule or has_sink_decomp_rule

        if lower:
            input_args = _prepare_python_api_arguments(fwd_op)
            pir.set_insertion_point(fwd_op)
            if has_sink_decomp_rule:
                decomp_outs = call_decomp(fwd_op)
                new_outs = _analyse_decomp_results(
                    orig_outs, decomp_outs, fwd_op
                )
            else:
                new_outs = _build_tensor_tuple(decom_rule(*input_args))

            _check_op_results(op_name, orig_outs, new_outs)

            # update_grad_var_to_var_map
            for grad_var, var in grad_var_to_var.items():
                if var in orig_outs:
                    grad_var_to_var[grad_var] = new_outs[orig_outs.index(var)]

            fwd_op.replace_all_uses_with(new_outs)
            block.remove_op(fwd_op)
            return new_outs, True
        else:
            return tuple(orig_outs), False


def _decomp_bwd_with_vjp(
    block: Block,
    fwd_op: pir.Operation,
    bwd_op: pir.Operation,
    grad_var_to_var: dict,
) -> tuple:
    '''
    Decompose the backward op into a list of primitive ops.
    If forward op has composite vjp rules (including custom vjp), call call_vjp() to get a list of primitive operators in backward graph, then replace backward op.

    Args:
        block (Block): the block to which the backward op belongs.
        fwd_op (pir.Operation): the forward op.
        bwd_op (pir.Operation): the backward op to be decomposed.
        grad_var_to_var_map (dict): a dict obtained from distributed processing,
            which maps the backward grad variable to its corresponding forward variable.
    Return:
        new_input_grads (tuple(Value)): new results of backward op after decomposing.
        has_decomposed: whether the backward op has been successfully decomposed. If a fwd op does not have composite vjp rules and can not be decomposed directly, this function will return False.
    '''

    def _prepare_output_grads(fwd_op, bwd_op):
        # check forward outputs and backward inputs
        fwd_outputs = fwd_op.results()
        fwd_output_names = fwd_op.get_output_names()
        assert len(fwd_output_names) == len(
            fwd_outputs
        ), "forward op output names do not match forward op outputs"
        bwd_inputs = [x.source() for x in bwd_op.operands()]
        bwd_input_names = bwd_op.get_input_names()
        assert len(bwd_input_names) == len(
            bwd_inputs
        ), "backward op input names do not match backward op inputs"

        # cut gradients from backward op's inputs
        fwd_inputs = [x.source() for x in fwd_op.operands()]
        grad_outputs = []
        grad_output_names = []
        for bwd_input in bwd_inputs:
            if not (bwd_input in fwd_inputs or bwd_input in fwd_outputs):
                grad_outputs.append([bwd_input])
                grad_output_names.append(
                    bwd_input_names[bwd_inputs.index(bwd_input)]
                )

        # add fake grads for forward op's outputs which are not used in backward op
        # this is necessary for the call_vjp(), which ensures that len(out_grads) must be equal to len(outputs)
        new_grad_outputs = []
        index = 0
        for fwd_output_name in fwd_output_names:
            if (fwd_output_name + "_grad") in grad_output_names:
                new_grad_outputs.append(grad_outputs[index])
                index += 1
            else:
                new_grad_outputs.append([pir.fake_op_result()])

        return new_grad_outputs

    fwd_inputs_ = [
        [fwd_op.operand_source(i)] for i in range(0, fwd_op.num_operands())
    ]
    fwd_outputs_ = [[fwd_output] for fwd_output in fwd_op.results()]
    grad_outputs_ = _prepare_output_grads(fwd_op, bwd_op)
    stop_gradients_ = []
    for grad_input in bwd_op.results():
        if grad_input.initialized():
            stop_gradients_.append([False])
        else:
            stop_gradients_.append([True])

    # record the backward op's position for subsequent replacement
    bwd_op_idx = block.ops.index(bwd_op)
    before_num_ops = len(block.ops)
    # generate primitive operators corresponding to the backward op
    new_grad_inputs = core.call_vjp(
        fwd_op, fwd_inputs_, fwd_outputs_, grad_outputs_, stop_gradients_
    )
    after_num_ops = len(block.ops)
    num_appended_ops = after_num_ops - before_num_ops

    # if forward op has no composite vjp rules, call_vjp() appends the same op as original backward op,
    # which means the backward op can not be decomposed directly, return False
    if num_appended_ops == 1 and block.ops[-1].name() == bwd_op.name():
        block.remove_op(block.ops[-1])
        return None, False
    else:
        # record new outputs of the decomposed backward op
        res = []
        for grad_input in new_grad_inputs:
            if grad_input[0] is not None and grad_input[0].initialized():
                res.append(grad_input[0])
            else:
                res.append(pir.fake_op_result())
        assert len(res) == len(
            bwd_op.results()
        ), "results of original backward op do not match results of decomposed backward op"

        # update_grad_var_to_var_map
        for idx, grad_input in enumerate(bwd_op.results()):
            if grad_input in grad_var_to_var.keys():
                grad_var_to_var[res[idx]] = grad_var_to_var.pop(grad_input)

        # move the list of primitive operators to the position of backward op
        insert_idx = bwd_op_idx
        for i in range(before_num_ops, after_num_ops):
            block.move_op(block.ops[i], insert_idx)
            insert_idx += 1

        # replace the following use of original backward op's outputs with new outputs, and then remove original backward op
        bwd_op.replace_all_uses_with(res)
        block.remove_op(bwd_op)

        return tuple(res), True


def _decomp_bwd_without_vjp(
    block: Block,
    bwd_op: pir.Operation,
    grad_var_to_var: dict,
    fwd_inputs: dict,
    fwd_outputs_after_decompose: tuple,
) -> tuple:
    '''
    Decompose the backward op into a list of primitive ops.
    If forward op has no composite vjp rules, and forward op has been decomposed to a list of primitive operators in forward graph previously,
    call grad() for the decomposed forward subgraph to get a list of primitive operators in backward graph, then replace backward op.

    Args:
        block (Block): the block to which the backawrd op belongs.
        bwd_op (pir.Operation): the backward op to be decomposed.
        grad_var_to_var (dict): a dict obtained from distributed processing,
            which maps the backward grad variable to its corresponding forward variable.
        fwd_inputs: (tuple(Value)): the original input of the forward op,
        fwd_outputs_after_decompose (tuple(Value)): the output of the decomposed forward op, if forward op has no vjp rules, forward op shoule be decomposed firstly,
            fwd_outputs_after_decompose means the new output of the decomposed forward op. If forward op has vjp rules, fwd_outputs_after_decompose is None.
    Return:
        new_input_grads (tuple(Value)): results of backward op after decomposing.
        has_decomposed: whether the backward op has been successfully decomposed.
    '''

    if fwd_outputs_after_decompose is None:
        raise RuntimeError(
            "To decompose backward op, please decompose forward op firstly"
        )

    # prepare forward and backward op's input and outputs infos
    bwd_inputs = [x.source() for x in bwd_op.operands()]
    grad_inputs = bwd_op.results()
    res = []

    # prepare the input args of grad(outputs, inputs, out_grads)
    grad_outputs = tuple(
        bwd_input
        for bwd_input in bwd_inputs
        if not (
            bwd_input in fwd_inputs or bwd_input in fwd_outputs_after_decompose
        )
    )
    fwd_outputs_ = tuple(
        grad_var_to_var[grad_output] for grad_output in grad_outputs
    )
    fwd_inputs_ = tuple(
        grad_var_to_var[grad_input]
        for grad_input in grad_inputs
        if grad_input.initialized()
    )

    # record the backward op's position for subsequent replacement
    bwd_op_idx = block.ops.index(bwd_op)
    before_num_ops = len(block.ops)
    # generate primitive operators corresponding to the backward op
    new_grad_inputs = ir_backward.grad(fwd_outputs_, fwd_inputs_, grad_outputs)
    after_num_ops = len(block.ops)

    # record new outputs of the decomposed backward op
    input_grads_idx = 0
    for idx, grad_input in enumerate(grad_inputs):
        if grad_input.initialized():
            res.append(new_grad_inputs[input_grads_idx])
            input_grads_idx += 1
        else:
            res.append(pir.fake_op_result())

    # update_grad_var_to_var_map
    for idx, grad_input in enumerate(grad_inputs):
        if grad_input in grad_var_to_var.keys():
            grad_var_to_var[res[idx]] = grad_var_to_var.pop(grad_input)

    # move the list of primitive operators to the position of backward op
    insert_idx = bwd_op_idx
    for i in range(before_num_ops, after_num_ops):
        block.move_op(block.ops[i], insert_idx)
        insert_idx += 1

    # replace the following use of original backward op's outputs with new outputs, and then remove original backward op
    bwd_op.replace_all_uses_with(res)
    block.remove_op(bwd_op)
    has_decomposed = True

    return tuple(res), has_decomposed


def _get_fwd_op(bwd_op, grad_var_to_var_map):
    bwd_op_input_names = bwd_op.get_input_names()
    out_grad_name = ["out_grad", "Out_grad", "loss_grad"]
    for idx, input_name in enumerate(bwd_op_input_names):
        if input_name in out_grad_name:
            out_grad = bwd_op.operand(idx).source()
            if out_grad in grad_var_to_var_map.keys():
                out = grad_var_to_var_map[out_grad]
                fwd_op = out.get_defining_op()
                return fwd_op
    return None


def _check_op(
    fwd_op: pir.Operation,
    bwd_op: pir.Operation,
):
    if fwd_op is None or fwd_op.name() + "_grad" != bwd_op.name():
        return False

    bwd_op_input_names = bwd_op.get_input_names()
    bwd_inputs = [x.source() for x in bwd_op.operands()]
    assert len(bwd_op_input_names) == len(
        bwd_inputs
    ), "backward op names do not match backward op inputs"
    fwd_op_related_inputs_outputs = []
    for idx, name in enumerate(bwd_op_input_names):
        if not ("_grad" in name):
            fwd_op_related_inputs_outputs.append(bwd_inputs[idx])
    fwd_inputs = [x.source() for x in fwd_op.operands()]
    fwd_outputs = fwd_op.results()
    for operand in fwd_op_related_inputs_outputs:
        if not (
            operand in fwd_inputs
            or operand in fwd_outputs
            or operand.get_defining_op().name() == "pd_op.full_int_array"
        ):
            return False

    return True


def _decomp_bwd_op(
    block: Block,
    bwd_op: pir.Operation,
    grad_var_to_var: dict,
):
    '''
    Decompose a backward op in pir program.
    Get the corresponding forward op according to grad_var_to_var firstly, then
    (1) try to decompose backward op by calling _decompose_bwd_with_vjp, if forward op has composite vjp rules (including custom vjp),
    _decompose_bwd_with_vjp will call call_vjp() to get a list of primitive operators in backward graph, then replace backward op successfully and return True;
    (2) when _decompose_bwd_with_vjp return False, means there is no composite vjp rules,
    try to decompose forward op firstly by calling _decompose_fwd_op firstly and get corresponding primitive operators in backward graph by calling _decompose_bwd_without_vjp secondly, then replace backward op successfully and return True;
    (3) if the backward op is still not decomposed by the above two steps, returns False.

    Args:
        block (Block): the block to which the backward op belongs.
        bwd_op (pir.Operation): the backward op to be decomposed.
        grad_var_to_var (dict): a dict obtained from distributed processing,
            which maps the backward grad variable to its corresponding forward variable.
    Return:
        new_input_grads (tuple(Value)): new results of backward op after decomposing.
        has_decomposed: whether the backward op has been successfully decomposed.
    '''

    if not core._is_bwd_prim_enabled():
        raise RuntimeError(
            "To decompose backward op, please set `core._set_prim_backward_enabled(True)` firstly"
        )
    # get the corresponding forward op according to grad_var_to_var
    # check and ensure: bwd_inputs = out_grads + fwd_inputs[optional] + fwd_outputs[optional]
    fwd_op = _get_fwd_op(bwd_op, grad_var_to_var)
    if not _check_op(fwd_op, bwd_op):
        return None, False

    # try to decompose backward op directly
    (
        new_grads,
        bwd_has_decomposed,
    ) = _decomp_bwd_with_vjp(
        block,
        fwd_op,
        bwd_op,
        grad_var_to_var,
    )

    if not bwd_has_decomposed:
        # try to decompose the forward op
        fwd_inputs = [x.source() for x in fwd_op.operands()]
        (
            new_fwd_outputs,
            fwd_has_decomposed,
        ) = _decomp_fwd_op(
            block,
            fwd_op,
            grad_var_to_var,
        )
        if fwd_has_decomposed:
            # try to decompose the backward op
            (
                new_grads,
                bwd_has_decomposed,
            ) = _decomp_bwd_without_vjp(
                block,
                bwd_op,
                grad_var_to_var,
                fwd_inputs,
                new_fwd_outputs,
            )

    return new_grads, bwd_has_decomposed


def decompose_pir_program(pir_program, param_mapping, grad_var_to_var):
    '''
    Decompose all backward ops in a pir program.

    Args:
        pir_program (Program): the program to be decomposed
        param_mapping (dict): a map of program variables to pir program opresults
        grad_var_to_var (dict): a dict obtained from distributed processing,
            which maps the backward grad variable to its corresponding forward variable.
    '''

    def _get_bwd_ops_name(pir_program):
        bwd_ops = []
        global_block = pir_program.global_block()
        for op in global_block.ops:
            if (
                op.name().endswith("_grad") or op.name().endswith("_grad_")
            ) and op.name() not in bwd_ops:
                bwd_ops.append(op.name())
        return bwd_ops

    def _translate_gradvartovar_to_pir(param_mapping, grad_var_to_var):
        pir_grad_var_to_var = {}
        for grad_var, var in grad_var_to_var.items():
            if grad_var in param_mapping.keys() and var in param_mapping.keys():
                if (
                    len(param_mapping[grad_var]) == 1
                    and len(param_mapping[var]) == 1
                ):
                    new_grad_var = param_mapping[grad_var][0]
                    new_var = param_mapping[var][0]
                    pir_grad_var_to_var[new_grad_var] = new_var
                else:
                    new_grad_vars = []
                    new_vars = []
                    if len(param_mapping[grad_var]) == 1:
                        new_grad_vars.append(param_mapping[grad_var][0])
                    elif (
                        len(param_mapping[grad_var]) == 2
                        and param_mapping[grad_var][1].get_defining_op().name()
                        == "builtin.slice"
                    ):
                        new_grad_vars.append(param_mapping[grad_var][1])
                    else:
                        last_op = param_mapping[grad_var][-1].get_defining_op()
                        if last_op.name().endswith("_"):
                            new_grad_vars.append(param_mapping[grad_var][0])

                    if len(param_mapping[var]) == 1:
                        new_vars.append(param_mapping[var][0])
                    elif (
                        len(param_mapping[var]) == 2
                        and param_mapping[var][1].get_defining_op().name()
                        == "builtin.slice"
                    ):
                        new_vars.append(param_mapping[var][1])
                    else:
                        last_op = param_mapping[var][-1].get_defining_op()
                        if last_op.name().endswith("_"):
                            new_vars.append(param_mapping[var][0])

                    assert (
                        len(new_grad_vars) == 1 and len(new_vars) == 1
                    ), "translate pir_grad_var_to_var error"
                    pir_grad_var_to_var[new_grad_vars[0]] = new_vars[0]
        return pir_grad_var_to_var

    prev_fwd_prim_state = core._is_fwd_prim_enabled()
    prev_bwd_prim_state = core._is_bwd_prim_enabled()
    core._set_prim_forward_enabled(True)
    core._set_prim_backward_enabled(True)
    prev_pir_api_flag = paddle.base.framework.get_flags("FLAGS_enable_pir_api")
    paddle.framework.set_flags(
        {"FLAGS_enable_pir_api": True}
    )  # set in pir mode for operator overloading

    with paddle.pir.core.program_guard(pir_program):
        pir_grad_var_to_var = _translate_gradvartovar_to_pir(
            param_mapping, grad_var_to_var
        )

        ops = pir_program.global_block().ops
        bwd_ops = _get_bwd_ops_name(pir_program)
        num_bwd_ops_decomposed = 0
        num_bwd_ops_undecomposed = 0
        bwd_ops_decomposed = []
        bwd_ops_undecomposed = []
        for op in ops:
            if op.name() in bwd_ops:
                new_grads, has_decomposed = _decomp_bwd_op(
                    pir_program.global_block(),
                    op,
                    pir_grad_var_to_var,
                )
                if has_decomposed:
                    num_bwd_ops_decomposed += 1
                    if op.name() not in bwd_ops_decomposed:
                        bwd_ops_decomposed.append(op.name())
                if not has_decomposed:
                    num_bwd_ops_undecomposed += 1
                    if op.name() not in bwd_ops_undecomposed:
                        bwd_ops_undecomposed.append(op.name())

        logging.getLogger().setLevel(logging.INFO)
        logging.info(
            "%d backward ops are successfully decomposed, op names are: %s"
            % (num_bwd_ops_decomposed, ', '.join(bwd_ops_decomposed))
        )
        logging.info(
            "%d backward ops can not be successfully decomposed, op names are: %s"
            % (
                num_bwd_ops_undecomposed,
                ', '.join(bwd_ops_undecomposed),
            )
        )

        core._set_prim_forward_enabled(prev_fwd_prim_state)
        core._set_prim_backward_enabled(prev_bwd_prim_state)
        paddle.base.framework.set_flags(prev_pir_api_flag)
        paddle.base.framework.global_var._use_pir_api_ = prev_pir_api_flag[
            "FLAGS_enable_pir_api"
        ]
