import subprocess
import os
import platform
import zipfile
import requests
import json
import logging
import random
import string


class Terraform:
    def __init__(
        self, work_dir: str,
        version="1.5.7",
        enable_plugin_cache=True,
        install_path="/opt/terraform",
        log_level="INFO",
        capture_output=False
    ) -> None:
        self._work_dir = work_dir
        self.workspace_name = os.path.basename(self._work_dir)
        self._install_path = os.path.abspath(install_path)
        self._binary_path = os.path.join(self._install_path, "terraform")
        self._capture_output = capture_output
        self._setloglevel(log_level)
        self.log(logging.INFO, f"Creating Terraform instance")
        self._plan_file = f"{self.workspace_name}.tfplan"

        os.environ.setdefault("TF_IN_AUTOMATION", "1")
        self.enable_plugin_cache = enable_plugin_cache
        if self.enable_plugin_cache:
            self._plugin_cache_dir = "/tmp/.terraform.d/plugin-cache"

            os.makedirs(self._plugin_cache_dir, exist_ok=True)
            os.environ["TF_PLUGIN_CACHE_DIR"] = self._plugin_cache_dir
            os.environ["CHECKPOINT_DISABLE"] = "true"
            self.log(
                logging.INFO, f"Plugin cache enabled, directory {self._plugin_cache_dir}")
        return None

    def _setloglevel(self, log_level: str) -> None:
        numeric_level = getattr(logging, log_level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % log_level)
        FORMAT = '%(asctime)s %(workspace)s %(message)s'
        logging.basicConfig(level=numeric_level, format=FORMAT)
        self.log(logging.INFO, f"Set log level to {log_level}")
        return None

    def log(self, log_level, message):
        d = {
            "workspace": self.workspace_name
        }
        if log_level == logging.INFO:
            logging.info(message, extra=d)
        elif log_level == logging.DEBUG:
            logging.debug(message, extra=d)
        elif log_level == logging.WARNING:
            logging.warning(message, extra=d)
        elif log_level == logging.CRITICAL:
            logging.critical(message, extra=d)

    def install(self, tf_version) -> None:
        architectures = {
            'aarch64': 'arm64',
            'x86_64': 'amd64'
        }
        operating_system = platform.system().lower()
        arch = architectures[platform.machine().lower()]
        os.makedirs(self._install_path, exist_ok=True)
        url = f"https://releases.hashicorp.com/terraform/{tf_version}/terraform_{tf_version}_{operating_system}_{arch}.zip"
        self.log(logging.INFO,
                 f"Downloading terraform binary from {url}")
        file_name = url.split("/")[-1]
        # Download the zip file
        tempfile = os.path.join("/tmp", file_name)
        response = requests.get(url)
        with open(tempfile, 'wb') as file:
            file.write(response.content)

        # Unzip the downloaded file
        with zipfile.ZipFile(tempfile, 'r') as zip_ref:
            zip_ref.extractall(self._install_path)
        os.chmod(self._binary_path, 0o755)
        os.remove(tempfile)
        return None

    def version(self) -> str:
        try:
            version = subprocess.run([self._binary_path, "version", "-json"],
                                     capture_output=True)
            version_json = json.loads(version.stdout)
            return version_json["terraform_version"]
        except Exception as e:
            raise Exception(e)

    def init(self, backend_config_file=None) -> str:
        try:
            init_conf_params = [self._binary_path, "init", "-input=false",
                                "-no-color"]
            if backend_config_file:
                init_conf_params.append(
                    f"-backend-config={backend_config_file}")
            p = subprocess.run(init_conf_params, cwd=self._work_dir,
                               capture_output=self._capture_output)
        except Exception as e:
            raise Exception(e)
        p.check_returncode()
        if self._capture_output:
            return p.stdout.decode('utf-8')

    def plan(self, destroy=False) -> str:
        plan_conf_params = [self._binary_path, "plan",
                            "-input=false", "-no-color", f"-out={self._plan_file}"]
        if destroy:
            plan_conf_params.append("-destroy")
        try:
            self.log(logging.INFO,
                     f"Plan for workspace {self._work_dir}, saving plan to {self._plan_file}")
            p = subprocess.run(plan_conf_params, cwd=self._work_dir,
                               capture_output=self._capture_output)
        except Exception as e:
            raise Exception(e)
        p.check_returncode()
        if self._capture_output:
            return p.stdout.decode('utf-8')

    def apply(self) -> str:
        if not os.path.exists(os.path.join(self._work_dir, self._plan_file)):
            self.log(logging.INFO,
                     f"Plan for the run {self.workspace_name} does not exist, performing plan")
            self.plan()
        try:
            self.log(logging.INFO,
                     f"Applying for workspace {self._work_dir}, plan file {self._plan_file}")
            p = subprocess.run([self._binary_path, "apply", "-input=false",
                                "-no-color", self._plan_file], cwd=self._work_dir, capture_output=self._capture_output)
        except Exception as e:
            raise Exception(e)
        p.check_returncode()
        if self._capture_output:
            return p.stdout.decode('utf-8')

    # @staticmethod
    # def get_human_readable_apply(json_apply_file):
    #     msg = ""
    #     with open(json_apply_file, mode="r") as json_fh:
    #         apply = []
    #         for l in json_fh:
    #             apply.append(json.loads(l))
    #         for l in apply:
    #             if l["type"] not in ('provision_complete', 'planned_change', 'version'):
    #                 msg += l["@message"]+'\n'
    #         return msg

    # @staticmethod
    # def get_apply_summary(json_apply_file):
    #     with open(json_apply_file, mode="r") as json_fh:
    #         for l in json_fh:
    #             row = json.loads(l)
    #             if row["type"] == 'change_summary':
    #                 return l["@message"]
