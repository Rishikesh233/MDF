"""
Functions for converting from MDF models version: mdf.0 to PyTorch
"""

import collections
import os
import sys
import h5py
import importlib
from collections import defaultdict
from inspect import getmembers, signature, getsource, isclass
import modeci_mdf
import numpy as np
import sympy
import torch
from typing import Union, Dict, Any, Tuple, List, Callable
import torch.nn as nn
from modeci_mdf.functions.standard import mdf_functions
from modeci_mdf.utils import load_mdf
from modeci_mdf.execution_engine import EvaluableGraph


not_self = set()
param_set = set()
graph_input = []


def get_module_declaration_text(
    name: str, node_dict: dict[Any, Any], execution_order: list[str]
):
    """
    Helper function to generate string in module.py. String will create an
    instance of the torch object corresponding to the node and function at node as method
    Generated text specifies the definition of class that will form the pytorch model,
    including __init__method and forward method.
    Returns string of the class definition with parameters and arguments assigned.

    Args:
        name: Name of the node
        node_dict: dictionary with attributes of the node such as Input Ports, functions, parameters and
        execution_order: List of nodes in the order of execution

    Returns: Script in PyTorch schema
    """
    declaration_text = ("\nclass {}(nn.Module):").format(name)

    input_ports = node_dict["input_ports"]
    functions = node_dict["functions"]
    parameters = node_dict["parameters"]
    output_ports = node_dict["output_ports"]

    function_set = set()
    for parameter in parameters:
        if parameter.function:
            function_set.add(parameter)

    if parameters and not functions:
        declaration_text += "\n\tdef __init__(self,"
        for parameter in parameters:
            if parameter.id == "input_level":
                graph_input.append(parameter.value)
            if not parameter.function:
                param_set.add(parameter.id)
            if not parameter.is_stateful() and not parameter.function:
                declaration_text += f"{parameter.id}=torch.tensor({parameter.value}),"
            elif parameter.is_stateful():
                if parameter.value:
                    declaration_text += "{}=torch.tensor({}),".format(parameter.id, 0)
                else:
                    if (
                        parameter.default_initial_value == "str"
                        and parameter.default_initial_value in param_set
                    ):
                        declaration_text += f"{parameter.id}=torch.tensor({parameter.default_initial_value.value}),"
                    else:
                        declaration_text += f"{parameter.id}=torch.tensor({parameter.default_initial_value}),"
        declaration_text += "):"
        declaration_text += "\n\t\tsuper().__init__()"
        for parameter in parameters:
            if not parameter.function:
                declaration_text += f"\n\t\tself.{parameter.id}={parameter.id}"
            elif parameter.function:
                function_set.add(parameter)

        declaration_text += "\n\t\tself.{}={}".format(
            "execution_count", "torch.tensor(0)"
        )
        # define the forward method of node
        declaration_text += "\n\tdef forward(self,"
        for input_port in input_ports:
            declaration_text += input_port.id

        declaration_text += " ):"
        declaration_text += "\n\t\tself.{}=self.{}".format(
            "execution_count", "execution_count+torch.tensor(1)"
        )
        # Handles the sateful parameter in forward method
        for parameter in parameters:
            if parameter.is_stateful() and not parameter.function:
                if parameter.value:
                    declaration_text += "\n\t\tself.{}={}".format(
                        parameter.id, sym(parameter.value)
                    )
                elif parameter.time_derivative:
                    declaration_text += "\n\t\tself.{}=self.{}+({})".format(
                        parameter.id, parameter.id, sym(parameter.time_derivative)
                    )
            # Handles the function at forward method
            if parameter.function:
                declaration_text += f"\n\t\t{parameter.id}="
                if parameter.function in mdf_functions:
                    exp = mdf_functions[parameter.function]["expression_string"]
                    exp = func_args(exp, parameter.args)
                    exp = sym(exp)

                declaration_text += exp
        # Value to be returned as output of forward method of each node
        if output_ports[0].value in not_self:
            declaration_text += "\n\t\treturn {}".format(output_ports[0].value)
        else:
            declaration_text += "\n\t\treturn {}".format(sym(output_ports[0].value))

    return declaration_text


# Add self to parameters to make it as attribute of pytorch node
def sym(value):
    for i in param_set:
        if i in value:
            value = value.replace(i, "self." + i)
    return value


def func_args(exp, arg_dict):
    for i in arg_dict:
        if i in exp:
            exp = exp.replace(i, str(arg_dict[i]))
    return exp


# Define the forward method of the main function--graph
def generate_main_forward(
    nodes: list["node"], execution_order: list[str], d_e: dict[str, Any]
):

    """Helper function to generate the main forward method that will specify
    the execution of the pytorch model. This requires proper ordering of module
    calls as well as preservation of variables.

    Args:
        nodes: list of nodes in the graph
        execution_order: List of nodes in the order of execution
        d_e: Weights on edges stored in dict format

    Returns: Function returns the main forward call that will be used to define pytorch
    model

    """
    node_dict = {node.id: node for node in nodes}

    d = {}
    main_forward = "\n\tdef forward(self, input):"
    if execution_order:
        for node in execution_order:
            main_forward += f"\n\t\t val_{node}=torch.zeros_like(input)"

        for node, dependency_set in d_e.items():
            if dependency_set == {}:
                main_forward += f"\n\n\t\t val_{node}=val_{node}+self.{node}()"
                d[node] = f"val_{node}"
            else:
                for k, v in dependency_set.items():
                    if v == None:
                        main_forward += (
                            f"\n\t\t val_{node}=val_{node}+self.{node}(val_{k})"
                        )
                    else:
                        main_forward += f"\n\t\t val_{node}=val_{node}+self.{node}(val_{k}*torch.tensor({v}))"
                    d[node] = f"val_{node}"

    main_forward += "\n\n\t\t return "

    for node in execution_order:
        main_forward += d[node] + ","

    return main_forward


# Create Script
def build_script(
    nodes: list["node"],
    execution_order: list[str],
    model_id1: str,
    d_e: dict[str, Any],
    conditions=None,
):
    """Helper function to create and assemble text components necessary to specify
    module.py importable model script.  These include:

            * Module declarations
                    * Initialization of functions
                    * Definition of forward function

            * Model main call declaration:
                    * Initialization of subcomponents
                    * Forward function logic

    Args:
        nodes: list of nodes in the graph
        execution_order: List of nodes in the order of execution
        d_e: Weights on edges stored in dict format
        model_id1: id of the model to be converted
        conditions:

    Returns: complete module.py script as a formatted string

    """
    model_id = model_id1 + ".onnx"
    script = ""
    imports_string = (
        "import torch"
        "\nimport torch.nn as nn\nimport onnx\nimport onnxruntime as rt\nfrom math import *"
    )

    # Declarations string
    modules_declaration_text = ""
    constructor_calls = {}
    declared_module_types = set()

    for node in nodes:
        id, funcs, params, out_ports, input_ports = (
            node.id,
            node.functions,
            node.parameters,
            node.output_ports,
            node.input_ports,
        )
        node_dict = {
            "functions": funcs,
            "parameters": params,
            "output_ports": out_ports,
            "input_ports": input_ports,
        }
        declaration_text = get_module_declaration_text(id, node_dict, execution_order)
        modules_declaration_text += declaration_text

    # Build Main call
    main_call_declaration = "\nclass Model(nn.Module):" "\n\tdef __init__(self,"
    for node in nodes:
        main_call_declaration += f"{node.id}" + ", "
    main_call_declaration += "):" "\n\t\tsuper().__init__()"

    for idx, node in enumerate(nodes):
        main_call_declaration += f"\n\t\tself.{node.id} = {node.id}"

    # Build Main forward
    main_call_forward = generate_main_forward(nodes, execution_order, d_e)
    script += imports_string
    script += modules_declaration_text
    script += main_call_declaration
    script += main_call_forward

    if len(nodes) == 1:
        script += "f\nmodel={nodes[0].id}"
        return script
    script += "\nmodel = Model("
    for node in nodes:
        script += f"{node.id}={node.id}(),"
    script += ")"
    script += "\nmodel=torch.jit.script(model)"
    if graph_input:
        script += f"\ndummy_input =torch.tensor{tuple(graph_input)}"
    else:
        script += f"\ndummy_input =torch.tensor(0.0)"
    script += "\noutput = model(dummy_input)"
    script += f"\ntorch.onnx.export(model,dummy_input,'{model_id}',verbose=True,input_names=[],example_outputs=output,opset_version=9)"
    script += f"\nonnx_model = onnx.load('{model_id}')"
    script += "\nonnx.checker.check_model(onnx_model)"
    script += f"\nsess = rt.InferenceSession('{model_id}')"
    script += "\nres = sess.run(None, {sess.get_inputs()[0].name: dummy_input.numpy()})"

    return script


def _generate_scripts_from_model(mdf_model: "Model") -> str:
    """Generating scripts from components of model
    Helper function to parse MDF objects from MDF json representation. Uses MDF scheduler to
    determine proper ordering of nodes, and calls `build_script`.

    Args: mdf_model: MDF model to be exported to PyTorch

    Returns: Returns dictionary of scripts where key = name of mdf model, value is string
    representation of script.

    """
    scripts = {}
    model_id1 = mdf_model.id
    for graph in mdf_model.graphs:
        nodes = graph.nodes
        evaluable_graph = EvaluableGraph(graph, verbose=False)
        enodes = evaluable_graph.enodes
        edges = evaluable_graph.ordered_edges
        try:
            conditions = evaluable_graph.conditions
        except AttributeError:
            conditions = {}

        # Use edges and nodes to construct execution order
        execution_order = []
        depend_dict = graph.dependency_dict
        d_e = {n.id: collections.defaultdict(dict) for n in graph.nodes}
        for graph in mdf_model.graphs:
            for edge in graph.edges:
                sender = graph.get_node(edge.sender)
                receiver = graph.get_node(edge.receiver)
                if edge.parameters and "weight" in edge.parameters:
                    d_e[receiver.id][sender.id] = edge.parameters["weight"]
                else:
                    d_e[receiver.id][sender.id] = None

        for idx, edge in enumerate(edges):
            if idx == 0:
                execution_order.append(edge.sender)
            execution_order.append(edge.receiver)
        if execution_order == []:
            for i in nodes:
                execution_order.append(i.id)

        # Build script
        script = build_script(
            nodes, execution_order, model_id1, d_e, conditions=conditions
        )
        scripts[graph.id] = script

    return scripts


# Generating PyTorch Script
def _script_to_model(script: str, model_id1: str):
    """
    Helper function to take the autogenerated module.py python script, and import
    it such that the pytorch model specified by this script is importable to the
    calling program.

    Returns torch.nn.Module object.
    """
    import importlib.util

    path_list = model_input.split("/")[:-2] + ["PyTorch/"]
    out_filename = "/".join(path_list)
    module_path = str(out_filename) + f"{model_id1}" + "_pytorch.py"

    with open(module_path, mode="w") as f:
        f.write(script)

    torch_spec = importlib.util.spec_from_file_location("module", module_path)
    torch_module = importlib.util.module_from_spec(torch_spec)
    torch_spec.loader.exec_module(torch_module)

    model = torch_module.model

    return model


def mdf_to_pytorch(mdf_model: "Model", eval_models=True):
    """Function loads and returns a pytorch model for all models specified in an
    mdf file
    Args:
        mdf_model: model in MDF format
        eval_models: Set Evaluation of model to True or False

    Returns: Returns a dictionary where key = model name, value = pytorch model object
    """
    scripts = _generate_scripts_from_model(mdf_model)
    models = {}

    for script_name, script in scripts.items():
        model = _script_to_model(script, mdf_model.id)
        if eval_models:
            model.eval()
        models[script_name] = model
    return models


__all__ = ["mdf_to_pytorch"]

if __name__ == "__main__":
    from pathlib import Path

    model_input = "C:/Users/mraunak/PycharmProjects/MDF/examples/PyTorch/Inception.json"
    mdf_model = load_mdf(model_input)
    pytorch_model = mdf_to_pytorch(mdf_model, eval_models=False)
