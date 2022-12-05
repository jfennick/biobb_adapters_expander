import argparse
import subprocess as sub
import sys
from pathlib import Path
from unittest.mock import patch
from typing import List

import graphviz
import networkx as nx
import pytest
import yaml
from networkx.algorithms import isomorphism

import wic.cli
import wic.compiler
import wic.main
import wic.utils
from wic import auto_gen_header
from wic.schemas import wic_schema
from wic.wic_types import GraphData, GraphReps, NodeData, StepId, Yaml, YamlTree


def get_args(yml_path: str = '') -> argparse.Namespace:
    """This is used to get mock command line arguments.

    Returns:
        argparse.Namespace: The mocked command line arguments
    """
    testargs = ['wic', '--yaml', yml_path, '--cwl_output_intermediate_files', 'True']  # ignore --yaml
    # For now, we need to enable --cwl_output_intermediate_files. See comment in compiler.py
    with patch.object(sys, 'argv', testargs):
        args: argparse.Namespace = wic.cli.parser.parse_args()
    return args


tools_cwl = wic.main.get_tools_cwl(get_args().cwl_dirs_file)
yml_paths = wic.main.get_yml_paths(get_args().yml_dirs_file)

yml_paths_tuples = [(yml_path_str, yml_path)
            for yml_namespace, yml_paths_dict in yml_paths.items()
            for yml_path_str, yml_path in yml_paths_dict.items()]

# Due to the computational complexity of the graph isomorphism problem, we
# need to manually exclude large workflows.
# See https://en.wikipedia.org/wiki/Graph_isomorphism_problem
large_workflows = ['dsb', 'dsb1', 'elm', 'vs_demo_2', 'vs_demo_3', 'vs_demo_4']
yml_paths_tuples_not_large = [(s, p) for (s, p) in yml_paths_tuples if s not in large_workflows]

# Generate schemas for validation
yaml_stems = [s for s, p in yml_paths_tuples]
validator = wic_schema.get_validator(tools_cwl, yaml_stems)


@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples)
def test_examples(yml_path_str: str, yml_path: Path) -> None:
    """Runs all of the examples in the examples/ directory. Note that some of
    the yml files lack inputs and cannot be run independently, and are excluded.
    """
    # First compile the workflow.
    # Load the high-level yaml workflow file.
    with open(yml_path, mode='r', encoding='utf-8') as y:
        root_yaml_tree: Yaml = yaml.safe_load(y.read())
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    wic_tag = {'wic': root_yaml_tree.get('wic', {})}
    plugin_ns = wic_tag['wic'].get('namespace', 'global')
    step_id = StepId(yml_path_str, plugin_ns)
    y_t = YamlTree(step_id, root_yaml_tree)
    yaml_tree_raw = wic.ast.read_ast_from_disk(y_t, yml_paths, tools_cwl, validator)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_raw.yml', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree_raw.yml))
    yaml_tree = wic.ast.merge_yml_trees(yaml_tree_raw, {}, tools_cwl)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_merged.yml', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree.yml))


    graph_gv = graphviz.Digraph(name=f'cluster_{yml_path}')
    graph_gv.attr(newrank='True')
    graph_nx = nx.DiGraph()
    graphdata = GraphData(str(yml_path))
    graph = GraphReps(graph_gv, graph_nx, graphdata)
    compiler_info = wic.compiler.compile_workflow(yaml_tree, get_args(str(yml_path)), [], [graph], {}, {}, {}, {},
                                                    tools_cwl, True, relative_run_path=True, testing=True)
    rose_tree = compiler_info.rose
    sub_node_data: NodeData = rose_tree.data
    yaml_stem = sub_node_data.name

    wic.utils.write_to_disk(rose_tree, Path('autogenerated/'), relative_run_path=True)

    yaml_inputs = rose_tree.data.workflow_inputs_file
    wic.main.stage_input_files(yaml_inputs, yml_path.parent.absolute())

    # Now blindly run all workflows and (if all inputs are present) check for return code 0.
    # Workflows are first validated before runtime, so this also checks for validity.
    # NOTE: Do not use --cachedir; we want to actually test everything.
    # NOTE: Using --leave-outputs because https://github.com/dnanexus/dx-cwl/issues/20
    cmd = ['cwltool', #'--outdir', f'outdir/{yaml_stem}',
            '--leave-tmpdir', '--leave-outputs',
            f'autogenerated/{yaml_stem}.cwl',
            f'autogenerated/{yaml_stem}_inputs.yml']
    proc = sub.run(cmd, stdout=sub.PIPE, stderr=sub.STDOUT, check=False)  # Capture the output
    if not proc.returncode == 0:
        # Since some of the workflows will be subworkflows
        # (i.e. will not have all inputs), we need to check for
        # "Missing required input parameter" and only fail the
        # workflows which should have succeeded.
        # TODO: Consider adding an explicit whitelist of workflows which should
        # compile and run successfully. The issue with relying on missing_input
        # only is that if a workflow previously compiled and executed, and then
        # either the compiler changed or someone modifies the workflow such that
        # there is now a missing input, that should be an error. Since we do not
        # have access to previous CI results, then we should have a whitelist.
        # More importantly, if we have a whitelist, we can get rid of the
        # "No definition found" error near line 560 of compiler.py
        # Otherwise, the error message will get buried in the subsequent stdout
        # and users will not notice it.
        missing_input = "Missing required input parameter"
        output = proc.stdout.decode("utf-8")
        if not missing_input in output:
            print(f"Error! {yml_path} failed!")
            print(output)
            assert proc.returncode == 0


@pytest.mark.fast
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_large)
def test_cwl_embedding_independence(yml_path_str: str, yml_path: Path) -> None:
    """Tests that compiling a subworkflow is independent of how it is embedded
    into a parent workflow. Specifically, this compiles the root workflow and
    re-compiles every subworkflow (individually) as if it were a root workflow,
    then checks that the CWL for each subworkflow remains identical and checks
    that the embedded subworkflow DAGs and the re-compiled DAGs are isomorphic.
    """
    # Load the high-level yaml workflow file.
    with open(yml_path, mode='r', encoding='utf-8') as y:
        root_yaml_tree: Yaml = yaml.safe_load(y.read())
    # Write the combined workflow (with all subworkflows as children) to disk.
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    wic_tag = {'wic': root_yaml_tree.get('wic', {})}
    plugin_ns = wic_tag['wic'].get('namespace', 'global')
    step_id = StepId(yml_path_str + '.yml', plugin_ns)
    y_t = YamlTree(step_id, root_yaml_tree)
    yaml_tree_raw = wic.ast.read_ast_from_disk(y_t, yml_paths, tools_cwl, validator)
    with open(f'autogenerated/{yml_path.stem}_tree_raw.yml', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree_raw.yml))
    yaml_tree = wic.ast.merge_yml_trees(yaml_tree_raw, {}, tools_cwl)
    with open(f'autogenerated/{yml_path.stem}_tree_merged.yml', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree.yml))

    # NOTE: The entire purpose of parsing an entire yaml forest is so we
    # can easily access the subtrees here. (i.e. without re-walking the AST)
    yaml_forest = wic.ast.tree_to_forest(yaml_tree, tools_cwl)
    yaml_forest_lst =  wic.utils.flatten_forest(yaml_forest)

    graph_gv = graphviz.Digraph(name=f'cluster_{yml_path}')
    graph_gv.attr(newrank='True')
    graph_nx = nx.DiGraph()
    graphdata = GraphData(str(yml_path))
    graph = GraphReps(graph_gv, graph_nx, graphdata)
    is_root = True
    compiler_info = wic.compiler.compile_workflow(yaml_tree, get_args(str(yml_path)), [], [graph], {}, {}, {}, {},
                                                    tools_cwl, is_root, relative_run_path=False, testing=True)
    rose_tree = compiler_info.rose
    node_data_lst: List[NodeData] = wic.utils.flatten_rose_tree(rose_tree)

    # This test doesn't necessarily need to write to disk, but useful for debugging.
    wic.utils.write_to_disk(rose_tree, Path('autogenerated/'), relative_run_path=False)

    # Now, for each subworkflow of the given root workflow, compile the
    # subworkflow again from scratch, as if it were the root workflow,
    # and check that the generated CWL is identical. In other words,
    # check that the generated CWL of a subworkflow is independent of its
    # embedding into a parent workflow.
    assert len(node_data_lst[1:]) == len(yaml_forest_lst)
    for sub_node_data, sub_yaml_forest in zip(node_data_lst[1:], yaml_forest_lst):
        sub_name = sub_node_data.name
        assert sub_yaml_forest.yaml_tree.step_id.stem == sub_name + '.yml'

        # NOTE: Do we want to also test embedding independence with args.graph_inline_depth?
        # If so, we will need to patch testargs depending on len(sub_node_data.namespaces)
        # (due to the various instances of `if len(namespaces) < args.graph_inline_depth`)

        graph_fakeroot_gv = graphviz.Digraph(name=f'cluster_{sub_name}')
        graph_fakeroot_gv.attr(newrank='True')
        graph_fakeroot_nx = nx.DiGraph()
        graphdata_fakeroot = GraphData(str(sub_name))
        graph_fakeroot = GraphReps(graph_fakeroot_gv, graph_fakeroot_nx, graphdata_fakeroot)
        fake_root = True
        compiler_info_fakeroot = wic.compiler.compile_workflow(sub_yaml_forest.yaml_tree, get_args(str(yml_path)),
            [], [graph_fakeroot], {}, {}, {}, {}, tools_cwl, fake_root, relative_run_path=False, testing=True)
        sub_node_data_fakeroot: NodeData = compiler_info_fakeroot.rose.data
        sub_cwl_fakeroot = sub_node_data_fakeroot.compiled_cwl

        # NOTE: Relative run: paths cause this test to fail, so remove them.
        # Using namespaced filenames in a single flat directory also
        # doesn't work because the namespaces will be of different lengths.
        sub_cwl_embedded = wic.utils.recursively_delete_dict_key('run', sub_node_data.compiled_cwl)
        sub_cwl_fakeroot = wic.utils.recursively_delete_dict_key('run', sub_cwl_fakeroot)

        if sub_cwl_embedded != sub_cwl_fakeroot:
            # Before we crash and burn, write out files for debugging.
            with open(f'{sub_name}_forest_embedded.yml', mode='w', encoding='utf-8') as w:
                w.write(yaml.dump(yaml_forest))
            with open(f'{sub_name}_forest_fakeroot.yml', mode='w', encoding='utf-8') as w:
                w.write(yaml.dump(sub_yaml_forest))
            # NOTE: Use _dot_cwl so we don't glob these files in get_tools_cwl()
            yaml_content = yaml.dump(sub_cwl_embedded, sort_keys=False, line_break='\n', indent=2)
            filename_emb = f'{sub_name}_embedded_dot_cwl'
            with open(filename_emb, mode='w', encoding='utf-8') as w:
                w.write('#!/usr/bin/env cwl-runner\n')
                w.write(auto_gen_header)
                w.write(''.join(yaml_content))
            yaml_content = yaml.dump(sub_cwl_fakeroot, sort_keys=False, line_break='\n', indent=2)
            filename_fake = f'{sub_name}_fakeroot_dot_cwl'
            with open(filename_fake, mode='w', encoding='utf-8') as w:
                w.write('#!/usr/bin/env cwl-runner\n')
                w.write(auto_gen_header)
                w.write(''.join(yaml_content))
            cmd = f'diff {filename_emb} {filename_fake} > {sub_name}.diff'
            sub.run(cmd, shell=True, check=False)
            print(f'Error! Check {filename_emb} and {filename_fake} and {sub_name}.diff')
        assert sub_cwl_embedded == sub_cwl_fakeroot

        # Check that the subgraphs are isomorphic.
        sub_graph_nx = sub_node_data.graph.networkx
        sub_graph_fakeroot_nx = sub_node_data_fakeroot.graph.networkx
        #assert isomorphism.faster_could_be_isomorphic(sub_graph_nx, sub_graph_fakeroot_nx)
        g_m = isomorphism.GraphMatcher(sub_graph_nx, sub_graph_fakeroot_nx)
        print('is_isomorphic()?', yml_path_str, sub_name)
        assert g_m.is_isomorphic() # See top-level comment above!


@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_large)
def test_inline_subworkflows(yml_path_str: str, yml_path: Path) -> None:
    """Tests that compiling a workflow is independent of how subworkflows are inlined.
    Specifically, this inlines every subworkflow (individually) and checks that
    the original DAG and the inlined DAGs are isomorphic.
    """
    # Load the high-level yaml workflow file.
    with open(yml_path, mode='r', encoding='utf-8') as y:
        root_yaml_tree: Yaml = yaml.safe_load(y.read())
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    wic_tag = {'wic': root_yaml_tree.get('wic', {})}
    plugin_ns = wic_tag['wic'].get('namespace', 'global')
    step_id = StepId(yml_path_str, plugin_ns)
    y_t = YamlTree(step_id, root_yaml_tree)
    yaml_tree_raw = wic.ast.read_ast_from_disk(y_t, yml_paths, tools_cwl, validator)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_raw.yml', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree_raw.yml))
    yaml_tree = wic.ast.merge_yml_trees(yaml_tree_raw, {}, tools_cwl)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_merged.yml', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree.yml))

    namespaces_list = wic.ast.get_inlineable_subworkflows(yaml_tree, tools_cwl, 'backend' in wic_tag, [])
    if namespaces_list == []:
        assert True # There's nothing to test

    graph_gv = graphviz.Digraph(name=f'cluster_{yml_path}')
    graph_gv.attr(newrank='True')
    graph_nx = nx.DiGraph()
    graphdata = GraphData(str(yml_path))
    graph = GraphReps(graph_gv, graph_nx, graphdata)
    compiler_info = wic.compiler.compile_workflow(yaml_tree, get_args(str(yml_path)), [], [graph], {}, {}, {}, {},
                                                    tools_cwl, True, relative_run_path=True, testing=True)
    rose_tree = compiler_info.rose
    sub_node_data: NodeData = rose_tree.data

    wic.utils.write_to_disk(rose_tree, Path('autogenerated/'), relative_run_path=True)

    # Inline each subworkflow individually and check that the graphs are isomorphic.
    for namespaces in namespaces_list:
        inline_yaml_tree = wic.ast.inline_subworkflow(yaml_tree, tools_cwl, namespaces)

        inline_graph_gv = graphviz.Digraph(name=f'cluster_{yml_path}')
        inline_graph_gv.attr(newrank='True')
        inline_graph_nx = nx.DiGraph()
        inline_graphdata = GraphData(str(yml_path))
        inline_graph = GraphReps(inline_graph_gv, inline_graph_nx, inline_graphdata)
        inline_compiler_info = wic.compiler.compile_workflow(inline_yaml_tree, get_args(str(yml_path)),
            [], [inline_graph], {}, {}, {}, {}, tools_cwl, True, relative_run_path=True, testing=True)
        inline_rose_tree = inline_compiler_info.rose
        inline_sub_node_data: NodeData = inline_rose_tree.data

        # Check that the subgraphs are isomorphic.
        sub_graph_nx = sub_node_data.graph.networkx
        sub_graph_fakeroot_nx = inline_sub_node_data.graph.networkx
        #assert isomorphism.faster_could_be_isomorphic(sub_graph_nx, sub_graph_fakeroot_nx)
        g_m = isomorphism.GraphMatcher(sub_graph_nx, sub_graph_fakeroot_nx)
        print('is_isomorphic()?', yml_path_str, namespaces)
        assert g_m.is_isomorphic() # See top-level comment above!
