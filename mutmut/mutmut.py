#!/usr/bin/env python
# -*- coding: utf-8 -*-

import importlib
import inspect
import os
import sys
import traceback
from io import (
    open,
)
from os.path import exists
from pathlib import Path
from shutil import copy
from time import time
from typing import List
import json 

import click
from glob2 import glob

from copy import copy as copy_obj

from mutmut import (
    mutate_file,
    MUTANT_STATUSES,
    Context,
    __version__,
    mutations_by_type,
    mutmut_config,
    config_from_file,
    guess_paths_to_mutate,
    Config,
    Progress,
    check_coverage_data_filepaths,
    popen_streaming_output,
    run_mutation_tests,
    read_coverage_data,
    read_patch_data,
    add_mutations_by_file,
    python_source_files,
    compute_exit_code,
    print_status,
    close_active_queues,
    mutate
)
from mutmut.cache import (
    create_html_report,
    cached_hash_of_tests,
)
from mutmut.cache import print_result_ids_cache, hash_of_tests, \
    filename_and_mutation_id_from_pk, cached_test_time, set_cached_test_time, \
    update_line_numbers, create_report


def do_apply(mutation_pk: str, dict_synonyms: List[str], backup: bool):
    """Apply a specified mutant to the source code

    :param mutation_pk: mutmut cache primary key of the mutant to apply
    :param dict_synonyms: list of synonym keywords for a python dictionary
    :param backup: if :obj:`True` create a backup of the source file
        before applying the mutation
    """
    filename, mutation_id = filename_and_mutation_id_from_pk(int(mutation_pk))

    update_line_numbers(filename)

    context = Context(
        mutation_id=mutation_id,
        filename=filename,
        dict_synonyms=dict_synonyms,
    )
    mutate_file(
        backup=backup,
        context=context,
    )


null_out = open(os.devnull, 'w')


class MutmutConfig:
    def __init__(self):
        self.argument = None
        self.paths_to_mutate = None
        self.disable_mutation_types = None
        self.enable_mutation_types = None
        self.runner = None
        self.tests_dir = None
        self.test_time_multiplier = 2.0
        self.test_time_base = 0.0
        self.swallow_output = None
        self.use_coverage = None
        self.dict_synonyms = "[Struct, NamedStruct]"
        self.pre_mutation = None
        self.post_mutation = None
        self.use_patch_file = None
        self.paths_to_exclude = ""
        self.simple_output = True
        self.no_progress = None
        self.ci = None
        self.rerun_all = None

        self.save_mutation_only=False
        self.save_mutation_to_folder=None
        self.line_start=0
        self.line_end=10000000

    def verify(self):
        if self.paths_to_mutate is None:
            raise AttributeError("paths_to_mutate is required")
        if self.runner is None:
            self.runner = "bash /usr/src/scripts/run_tests.sh"
        if self.tests_dir is None:
            self.tests_dir = os.environ['PROJECT_ROOT']

def run(config):
    config.verify()
    do_run(config.argument, config.paths_to_mutate, config.disable_mutation_types, config.enable_mutation_types, config.runner,
           config.tests_dir, config.test_time_multiplier, config.test_time_base, config.swallow_output, config.use_coverage,
           config.dict_synonyms, config.pre_mutation, config.post_mutation, config.use_patch_file, config.paths_to_exclude,
           config.simple_output, config.no_progress, config.ci, config.rerun_all,
           save_mutation_only=config.save_mutation_only, save_mutation_to_folder=config.save_mutation_to_folder, line_start=config.line_start, line_end=config.line_end)


def result_ids(status):
    """
    Print the IDs of the specified mutant classes (separated by spaces).\n
    result-ids survived (or any other of: killed,timeout,suspicious,skipped,untested)\n
    """
    if not status or status not in MUTANT_STATUSES:
        raise click.BadArgumentUsage(f'The result-ids command needs a status class of mutants '
                                     f'(one of : {set(MUTANT_STATUSES.keys())}) but was {status}')
    print_result_ids_cache(status)
    sys.exit(0)



def html(dict_synonyms, directory):
    """
    Generate a HTML report of surviving mutants.
    """
    create_html_report(dict_synonyms, directory)


def do_run(
    argument,
    paths_to_mutate,
    disable_mutation_types,
    enable_mutation_types,
    runner,
    tests_dir,
    test_time_multiplier,
    test_time_base,
    swallow_output,
    use_coverage,
    dict_synonyms,
    pre_mutation,
    post_mutation,
    use_patch_file,
    paths_to_exclude,
    simple_output,
    no_progress,
    ci,
    rerun_all,
    save_mutation_only=False,
    save_mutation_to_folder=None,
    line_start=0,
    line_end=100000000, # large number to avoid skipping any lines
) -> int:
    """return exit code, after performing an mutation test run.

    :return: the exit code from executing the mutation tests for run command
    """
    if use_coverage and use_patch_file:
        raise click.BadArgumentUsage("You can't combine --use-coverage and --use-patch")

    if disable_mutation_types and enable_mutation_types:
        raise click.BadArgumentUsage("You can't combine --disable-mutation-types and --enable-mutation-types")
    if enable_mutation_types:
        mutation_types_to_apply = set(mtype.strip() for mtype in enable_mutation_types.split(","))
        invalid_types = [mtype for mtype in mutation_types_to_apply if mtype not in mutations_by_type]
    elif disable_mutation_types:
        mutation_types_to_apply = set(mutations_by_type.keys()) - set(mtype.strip() for mtype in disable_mutation_types.split(","))
        invalid_types = [mtype for mtype in disable_mutation_types.split(",") if mtype not in mutations_by_type]
    else:
        mutation_types_to_apply = set(mutations_by_type.keys())
        invalid_types = None
    if invalid_types:
        raise click.BadArgumentUsage(f"The following are not valid mutation types: {', '.join(sorted(invalid_types))}. Valid mutation types are: {', '.join(mutations_by_type.keys())}")

    dict_synonyms = [x.strip() for x in dict_synonyms.split(',')]

    if use_coverage and not exists('.coverage'):
        raise FileNotFoundError('No .coverage file found. You must generate a coverage file to use this feature.')

    if paths_to_mutate is None:
        paths_to_mutate = guess_paths_to_mutate()

    def split_paths(paths):
        # This method is used to split paths that are separated by commas or colons
        for sep in [',', ':']:
            separated = list(filter(lambda p: Path(p).exists(), paths.split(sep)))
            if separated:
                return separated
        return None

    if not isinstance(paths_to_mutate, (list, tuple)):
        # If the paths_to_mutate is a string, we split it by commas or colons
        paths_to_mutate = split_paths(paths_to_mutate)

    if not paths_to_mutate:
        raise click.BadOptionUsage(
            '--paths-to-mutate',
            'You must specify a list of paths to mutate.'
            'Either as a command line argument, or by setting paths_to_mutate under the section [mutmut] in setup.cfg.'
            'To specify multiple paths, separate them with commas or colons (i.e: --paths-to-mutate=path1/,path2/path3/,path4/).'
        )

    tests_dirs = []
    test_paths = split_paths(tests_dir)
    if test_paths is None:
        raise FileNotFoundError(
            'No test folders found in current folder. Run this where there is a "tests" or "test" folder.'
        )
    for p in test_paths:
        tests_dirs.extend(glob(p, recursive=True))

    for p in paths_to_mutate:
        for pt in split_paths(tests_dir):
            tests_dirs.extend(glob(p + '/**/' + pt, recursive=True))
    del tests_dir
    current_hash_of_tests = hash_of_tests(tests_dirs)

    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'  # stop python from creating .pyc files

    using_testmon = '--testmon' in runner
    output_legend = {
        "killed": "🎉",
        "timeout": "⏰",
        "suspicious": "🤔",
        "survived": "🙁",
        "skipped": "🔇",
    }
    if simple_output:
        output_legend = {key: key.upper() for (key, value) in output_legend.items()}


    if hasattr(mutmut_config, 'init'):
        mutmut_config.init()
        
    baseline_time_elapsed = time_test_suite(
        swallow_output=not swallow_output,
        test_command=runner,
        using_testmon=using_testmon,
        current_hash_of_tests=current_hash_of_tests,
        no_progress=no_progress,
    )    

    if using_testmon:
        copy('.testmondata', '.testmondata-initial')

    # if we're running in a mode with externally whitelisted lines
    covered_lines_by_filename = None
    coverage_data = None
    if use_coverage or use_patch_file:
        covered_lines_by_filename = {}
        if use_coverage:
            coverage_data = read_coverage_data()
            check_coverage_data_filepaths(coverage_data)
        else:
            assert use_patch_file
            covered_lines_by_filename = read_patch_data(use_patch_file)

    mutations_by_file = {}

    paths_to_exclude = paths_to_exclude or ''
    if paths_to_exclude:
        paths_to_exclude = [path.strip() for path in paths_to_exclude.replace(',', '\n').split('\n')]
        paths_to_exclude = [x for x in paths_to_exclude if x]

    config = Config(
        total=0,  # we'll fill this in later!
        swallow_output=not swallow_output,
        test_command=runner,
        covered_lines_by_filename=covered_lines_by_filename,
        coverage_data=coverage_data,
        baseline_time_elapsed=baseline_time_elapsed,
        test_timeout_multiplier=3,
        test_timeout_max=60 * 3,
        dict_synonyms=dict_synonyms,
        using_testmon=using_testmon,
        tests_dirs=tests_dirs,
        hash_of_tests=current_hash_of_tests,
        test_time_multiplier=test_time_multiplier,
        test_time_base=test_time_base,
        pre_mutation=pre_mutation,
        post_mutation=post_mutation,
        paths_to_mutate=paths_to_mutate,
        mutation_types_to_apply=mutation_types_to_apply,
        no_progress=no_progress,
        ci=ci,
        rerun_all=rerun_all
    )

    parse_run_argument(argument, config, dict_synonyms, mutations_by_file, paths_to_exclude, paths_to_mutate, tests_dirs)
    
    if len(paths_to_mutate) == 1:
        path_to_mutate_ = paths_to_mutate[0]

        mut = mutations_by_file[path_to_mutate_]
        mutations_by_file[path_to_mutate_] = []
        for m in mut:
            if line_start <= m.line_number < line_end:
                mutations_by_file[path_to_mutate_].append(m)

        if save_mutation_only:
            with open(path_to_mutate_) as f:
                source = f.read()
            for i, id in enumerate(mutations_by_file[path_to_mutate_]):
                context = Context(
                    mutation_id=id,
                    filename=path_to_mutate_,
                    dict_synonyms=config.dict_synonyms,
                    config=copy_obj(config),
                    source=source,
                    index=i,
                )
                mutate(context)
                if save_mutation_to_folder:
                    with open(os.path.join(save_mutation_to_folder, f"mutate_{i}.py"), 'w') as f:
                        f.write(context.mutated_source)
            return 0

    config.total = sum(len(mutations) for mutations in mutations_by_file.values())

    print()
    print('2. Checking mutants')
    progress = Progress(total=config.total, output_legend=output_legend, no_progress=no_progress)

    try:
        run_mutation_tests(config=config, progress=progress, mutations_by_file=mutations_by_file)
    except Exception as e:
        traceback.print_exc()
        return compute_exit_code(progress, e)
    else:
        return compute_exit_code(progress, ci=ci)
    finally:
        print()  # make sure we end the output with a newline
        close_active_queues()


def parse_run_argument(argument, config, dict_synonyms, mutations_by_file, paths_to_exclude, paths_to_mutate, tests_dirs):
    if argument is None:
        for path in paths_to_mutate:
            for filename in python_source_files(path, tests_dirs, paths_to_exclude):
                if filename.startswith('test_') or filename.endswith('__tests.py'):
                    continue
                update_line_numbers(filename)
                add_mutations_by_file(mutations_by_file, filename, dict_synonyms, config)
    else:
        try:
            int(argument)
        except ValueError:
            filename = argument
            if not os.path.exists(filename):
                raise click.BadArgumentUsage('The run command takes either an integer that is the mutation id or a path to a file to mutate')
            update_line_numbers(filename)
            add_mutations_by_file(mutations_by_file, filename, dict_synonyms, config)
            return

        filename, mutation_id = filename_and_mutation_id_from_pk(int(argument))
        update_line_numbers(filename)
        mutations_by_file[filename] = [mutation_id]


def time_test_suite(
    swallow_output: bool,
    test_command: str,
    using_testmon: bool,
    current_hash_of_tests,
    no_progress,
) -> float:
    """Execute a test suite specified by ``test_command`` and record
    the time it took to execute the test suite as a floating point number

    :param swallow_output: if :obj:`True` test stdout will be not be printed
    :param test_command: command to spawn the testing subprocess
    :param using_testmon: if :obj:`True` the test return code evaluation will
        accommodate for ``pytest-testmon``

    :return: execution time of the test suite
    """
    cached_time = cached_test_time()
    if cached_time is not None and current_hash_of_tests == cached_hash_of_tests():
        print('1. Using cached time for baseline tests, to run baseline again delete the cache file')
        return cached_time

    print('1. Running tests without mutations')
    start_time = time()

    output = []

    def feedback(line):
        if not swallow_output:
            print(line)
        if not no_progress:
            print_status('Running...')
        output.append(line)

    returncode = popen_streaming_output(test_command, feedback)

    if returncode == 0 or (using_testmon and returncode == 5):
        baseline_time_elapsed = time() - start_time
    else:
        raise RuntimeError("Tests don't run cleanly without mutations. Test command was: {}\n\nOutput:\n\n{}".format(test_command, '\n'.join(output)))

    print('Done')

    set_cached_test_time(baseline_time_elapsed, current_hash_of_tests)

    return baseline_time_elapsed




