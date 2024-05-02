import shutil
import multiprocessing as mp
import sys
from subprocess import CalledProcessError
# from multiprocessing import Process, Lock
from terraform import Terraform


def safe_print(lock, msg, workspace, action):
    f = format_msg(msg, workspace, action)
    lock.acquire()
    print(f)
    lock.release()


def format_msg(msg, tf_workspace_name, tf_stage):
    from textwrap import indent

    msg = indent(msg, "  ")
    maxlen = 0
    for row in msg.split("\n"):
        maxlen = max(len(row), maxlen)
    return f"""
{'='*(len(tf_workspace_name)+len(tf_stage)+3)}
{tf_workspace_name} - {tf_stage}
{'='*(len(tf_workspace_name)+len(tf_stage)+3)}
{msg}
{'='*(maxlen)}
"""


def print_header(tf_workspace_name, tf_stage):
    print(f"\n{'='*(len(tf_workspace_name)+len(tf_stage)+3)}")
    print(f"{tf_workspace_name} - {tf_stage}")
    print(f"{'='*(len(tf_workspace_name)+len(tf_stage)+3)}")
    sys.stdout.flush()


def tf_init(lock, workspace, tf_install_path, backend_config_file):
    tf = Terraform(work_dir=workspace, log_level="CRITICAL",
                   install_path=tf_install_path, capture_output=True)
    try:
        init_output = tf.init(backend_config_file=backend_config_file)
        safe_print(lock, init_output, tf.workspace_name, "init")
    except CalledProcessError as e1:
        safe_print(lock, e1.stderr.decode('utf-8'),
                   tf.workspace_name, "init")
        sys.exit(1)
    except Exception as e2:
        safe_print(lock, e2)
        sys.exit(1)


def tf_plan(lock, workspace, tf_install_path, destroy=False):
    tf = Terraform(work_dir=workspace, log_level="CRITICAL",
                   install_path=tf_install_path, capture_output=True)
    try:
        plan_output = tf.plan(destroy)
        safe_print(lock, plan_output, tf.workspace_name, "plan")
    except CalledProcessError as e1:
        safe_print(lock, e1.stderr.decode('utf-8'),
                   tf.workspace_name, "plan")
        sys.exit(1)
    except Exception as e2:
        safe_print(lock, e2)
        sys.exit(1)


def tf_apply(lock, workspace, tf_install_path):
    tf = Terraform(work_dir=workspace, log_level="CRITICAL",
                   install_path=tf_install_path, capture_output=True)
    try:
        apply_output = tf.apply()
        safe_print(lock, apply_output, tf.workspace_name, "apply")
    except CalledProcessError as e1:
        safe_print(lock, e1.stderr.decode('utf-8'),
                   tf.workspace_name, "apply")
        sys.exit(1)
    except Exception as e2:
        safe_print(lock, e2)
        sys.exit(1)


def main():
    import argparse
    import os
    import time

    parser = argparse.ArgumentParser(
        description="A simple script to demonstrate argparse")

    parser.add_argument('-s', '--stage', required=True, choices=['install', 'init', 'plan',
                        'apply'], help="Stage to execute (install, init, plan, apply)")
    parser.add_argument('-p', '--enable-parallelism',
                        action='store_true', help="Enable parallelism")
    parser.add_argument('-t', '--terraform-version',
                        help="Terraform version to use")
    parser.add_argument('-b', '--backend-config-file',
                        help="Backend configuration file for init stage")
    parser.add_argument('-w', '--workspaces', required=True, nargs='+', type=str,
                        help="List of workspace directories")

    args = parser.parse_args()
    tf_stage = args.stage
    if args.terraform_version:
        tf_version = args.terraform_version
    else:
        tf_version = os.environ.get("TERRAFORM_VERSION", "1.7.5")
    workspaces = args.workspaces
    tf_install_path = "./terraform"
    child_processes = {}

    if tf_stage == "install":
        tf = Terraform(work_dir="", log_level="CRITICAL",
                       install_path=tf_install_path, capture_output=True)
        tf.install(tf_version)

    # perform the first init that populates the plugin cache,
    # then copy the resulting .terraform.lock.hcl file in all other workspaces.
    # This prevents terraform from downloading the plugin files for each workspace,
    # due to https://github.com/hashicorp/terraform/pull/32129.
    # Starting from version 1.4, terraform keeps downloading the plugin files
    # even if they are already present in the cache if the dependency lock file is missing.
    elif tf_stage == "init":
        first_init = workspaces[0]
        tf = Terraform(work_dir=first_init, log_level="CRITICAL",
                       install_path=tf_install_path, capture_output=False, enable_plugin_cache=True)
        print_header(tf.workspace_name, tf_stage)
        try:
            tf.init(args.backend_config_file)
        except Exception as e:
            sys.exit(1)
        saved_dependency_lock_file = "terraform.lock.hcl"
        shutil.copy(f"{first_init}/.terraform.lock.hcl",
                    saved_dependency_lock_file)

        # we skip the first workspace that's been already inited
        for d in workspaces[1:]:
            shutil.copy(saved_dependency_lock_file, f"{d}/.terraform.lock.hcl")
            if args.enable_parallelism:
                child_processes[d] = mp.Process(target=tf_init, args=(
                    mp.Lock(), d, tf_install_path, args.backend_config_file))
            else:
                tf = Terraform(work_dir=d, log_level="CRITICAL",
                               install_path=tf_install_path, capture_output=False, enable_plugin_cache=True)
                print_header(tf.workspace_name, tf_stage)
                try:
                    tf.init(args.backend_config_file)
                except Exception as e:
                    sys.exit(1)

    elif tf_stage == "plan":
        for d in workspaces:
            if args.enable_parallelism:
                child_processes[d] = mp.Process(target=tf_plan, args=(
                    mp.Lock(), d, tf_install_path))
            else:
                tf = Terraform(work_dir=d, log_level="CRITICAL",
                               install_path=tf_install_path, capture_output=False)
                print_header(tf.workspace_name, tf_stage)
                try:
                    tf.plan()
                except Exception as e:
                    sys.exit(1)

    elif tf_stage == "apply":
        for d in workspaces:
            if args.enable_parallelism:
                child_processes[d] = mp.Process(target=tf_apply, args=(
                    mp.Lock(), d, tf_install_path))
            else:
                tf = Terraform(work_dir=d, log_level="CRITICAL",
                               install_path=tf_install_path, capture_output=False)
                print_header(tf.workspace_name, tf_stage)
                try:
                    tf.apply()
                except Exception as e:
                    sys.exit(1)

    # elif tf_stage == "plandestroy":
    #     for d in workspaces:
    #         if args.enable_parallelism:
    #             child_processes[d] = mp.Process(target=tf_plan, args=(
    #                 mp.Lock(), d, tf_install_path, True))
    #         else:
    #             tf = Terraform(work_dir=d, log_level="CRITICAL",
    #                            install_path=tf_install_path, capture_output=False)
    #             print_header(tf.workspace_name, tf_stage)
    #             tf.plan()

    if args.enable_parallelism:
        mem_mb = os.sysconf('SC_PAGE_SIZE') * \
            os.sysconf('SC_PHYS_PAGES') / (1024.**2)
        mem_per_process = os.environ.get("MEM_PER_PROCESS", 512)
        max_processes = os.environ.get(
            "MAX_PROCESSES", int(mem_mb/mem_per_process))
        waiting_processes = list(child_processes.keys())
        failed_processes = []
        running_processes = []
        completed_processes = 0
        loop_count = 0
        while True:
            for d in child_processes.keys():
                if d in waiting_processes:
                    if len(running_processes) >= max_processes:
                        continue
                    print(f"Starting {d}...")
                    running_processes.append(d)
                    waiting_processes.remove(d)
                    child_processes[d].start()
                elif d in running_processes:
                    if child_processes[d].is_alive():
                        continue
                    else:
                        if child_processes[d].exitcode != 0:
                            print(f"{d} had errors...", flush=True)
                            failed_processes.append(d)
                        else:
                            print(f"{d} completed successfully...", flush=True)
                        running_processes.remove(d)
                        completed_processes += 1

            if len(running_processes) == 0 and len(waiting_processes) == 0:
                break
            else:
                if loop_count % 20 == 0:
                    print(
                        f"Running: {len(running_processes)} - Waiting: {len(waiting_processes)} - Completed: {completed_processes} - Failed: {len(failed_processes)}", flush=True)
                loop_count += 1
                time.sleep(1)

        print(f"\n\n{'='*7}")
        print("Summary")
        print(f"{'='*7}\n")
        print(f"* {len(workspaces) - len(failed_processes)}/{len(workspaces)} terraform {tf_stage} completed successfully")

        if len(failed_processes) > 0:
            print(f"* {len(failed_processes)}/{len(workspaces)} failed")
            for d in failed_processes:
                print(f"  - workspace {d}")
            sys.exit(1)


if __name__ == '__main__':
    main()
