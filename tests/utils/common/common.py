#!/usr/bin/python
# Copyright 2022 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from distutils.version import LooseVersion
import fcntl
import filelock
import pytest
import os
import re
import subprocess
import time
import tempfile
import shutil
import signal
import sys

from contextlib import contextmanager
import traceback


class Result:
    def __init__(self, stdout, stderr, exited):
        self.stdout = stdout
        self.stderr = stderr
        self.exited = exited
        self.return_code = exited


class Connection:
    def __init__(self, host, user, port, connect_timeout, connect_kwargs={}):
        self.host = host
        self.user = user
        self.port = port
        self.connect_timeout = connect_timeout
        self.connect_kwargs = connect_kwargs

        self.key_filename = None

        for k in connect_kwargs.keys():
            if k == "key_filename":
                self.key_filename = connect_kwargs[k]
            else:
                raise NotImplementedError(f"Argument {k} is not implemented")

    def get_connect_args(self):
        if self.key_filename is not None:
            key_arg = ["-i", self.key_filename]
        else:
            key_arg = []

        args = (
            ["ssh"]
            + key_arg
            + [
                "-p",
                str(self.port),
                "-o",
                f"ConnectTimeout={self.connect_timeout}",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "StrictHostKeyChecking=no",
                f"{self.user}@{self.host}",
            ]
        )

        return args

    def run(self, command, warn=False, hide=False, echo=True, popen=False):
        ssh_command = self.get_connect_args() + [command]

        if echo:
            print(command)

        if popen:
            return subprocess.Popen(ssh_command)
        else:
            try:
                proc = subprocess.run(ssh_command, check=not warn, capture_output=True)
                returncode = proc.returncode
            except subprocess.CalledProcessError as e:
                returncode = e.returncode
                if returncode != 255:
                    raise

            if returncode == 255:
                raise ConnectionError(
                    f"Could not connect using command '{ssh_command}'"
                )

            stdout = proc.stdout.decode()
            stderr = proc.stderr.decode()

            if not hide:
                print(stdout)
                print(stderr)

            return Result(stdout, stderr, returncode)

    def local(self, command, warn=False):
        return subprocess.run(command, shell=True, check=not warn)


# Copied from filelock.py. The original code is public domain, so it's ok to
# relicense. The only change from the original is the usage of LOCK_SH instead
# of LOCK_EX.
class ReadFileLock(filelock.BaseFileLock):
    """
    Uses the :func:`fcntl.flock` to hard lock the lock file on unix systems.
    """

    def _acquire(self):
        open_mode = os.O_RDWR | os.O_CREAT | os.O_TRUNC
        fd = os.open(self._lock_file, open_mode)

        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except (IOError, OSError):
            os.close(fd)
        else:
            self._lock_file_fd = fd
        return None

    def _release(self):
        # Do not remove the lockfile:
        #
        #   https://github.com/benediktschmitt/py-filelock/issues/31
        #   https://stackoverflow.com/questions/17708885/flock-removing-locked-file-without-race-condition
        fd = self._lock_file_fd
        self._lock_file_fd = None
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return None


WriteFileLock = filelock.FileLock


def get_worker_count():
    count = os.getenv("PYTEST_XDIST_WORKER_COUNT")
    if count is not None:
        return int(count)
    # Default to one worker.
    return 1


def get_worker_index():
    worker = os.getenv("PYTEST_XDIST_WORKER")
    if worker is not None:
        match = re.search("[0-9]+$", worker)
        return int(match.group(0))
    # Default to zero = one worker.
    return 0


def _start_qemu(qenv, conn, qemu_wrapper):
    """Start QEMU and return a subprocess.Popen object corresponding to a running
    qemu process.

    Parameters:
    * qenv is a dict of environment variables that will be added to
      subprocess.Popen(..,env=).
    * conn is a Fabric connection object to run SSH commands in the device.
    * qemu_rwapper is an string with the path to the wrapper to launch QEMU.

    Once qemu is started, a connection over ssh will attempted, so the returned
    process is actually a QEMU instance with a fully booted guest OS.

    The helper uses `meta-mender-qemu/scripts/mender-qemu` to start qemu, thus
    you can use `VEXPRESS_IMG`, `QEMU_DRIVE` and other environment variables to
    override the default behavior.
    """
    env = dict(os.environ)
    env.update(qenv)

    env["PORT_NUMBER"] = str(8822 + get_worker_index())
    env["VNC_NUMBER"] = str(23 + get_worker_index())

    proc = subprocess.Popen([qemu_wrapper], env=env, start_new_session=True)

    try:
        # make sure we are connected.
        run_after_connect("true", conn)
    except:
        # or do the necessary cleanup if we're not
        try:
            # qemu might have exited and this would raise an exception
            print("terminating qemu wrapper with pid {}".format(proc.pid))
            proc.terminate()
        except:
            pass

        proc.wait()
        raise

    return proc


def start_qemu_block_storage(latest_sdimg, suffix, conn, qemu_wrapper):
    """Start qemu instance running block storage"""
    fh, img_path = tempfile.mkstemp(suffix=suffix, prefix="test-image")
    # don't need an open fd to temp file
    os.close(fh)

    # Make a disposable image.
    shutil.copy(latest_sdimg, img_path)

    # pass QEMU drive directly
    qenv = {}
    qenv["DISK_IMG"] = img_path

    try:
        qemu = _start_qemu(qenv, conn, qemu_wrapper)
    except:
        # If qemu failed to start, remove the image and exit; else the image
        # shall be cleaned up by the caller
        os.remove(img_path)
        raise

    return qemu, img_path


def start_qemu_flash(latest_vexpress_nor, conn, qemu_wrapper):
    """Start qemu instance running *.vexpress-nor image"""

    print("qemu raw flash with image {}".format(latest_vexpress_nor))

    # make a temp file, make sure that it has .vexpress-nor suffix, so that
    # mender-qemu will know how to handle it
    fh, img_path = tempfile.mkstemp(suffix=".vexpress-nor", prefix="test-image")
    # don't need an open fd to temp file
    os.close(fh)

    # vexpress-nor is more complex than sdimg, inside it's compose of 2 raw
    # files that represent 2 separate flash banks (and each file is a 'drive'
    # passed to qemu). Because of this, we cannot directly apply qemu-img and
    # create a qcow2 image with backing file. Instead make a disposable copy of
    # flash image file.
    shutil.copyfile(latest_vexpress_nor, img_path)

    qenv = {}
    # pass QEMU drive directly
    qenv["DISK_IMG"] = img_path
    qenv["MACHINE"] = "vexpress-qemu-flash"

    try:
        qemu = _start_qemu(qenv, conn, qemu_wrapper)
    except:
        # If qemu failed to start, remove the image and exit; else the image
        # shall be cleaned up by the caller
        os.remove(img_path)
        raise

    return qemu, img_path


def reboot(conn, wait=360):
    try:
        conn.run("reboot", warn=True)
    except:
        # qemux86-64 is so fast that sometimes the above call fails with
        # an exception because the connection was broken before we returned.
        # So catch everything, even though it might hide real errors (but
        # those will probably be caught below after the timeout).
        pass

    # Make sure reboot has had time to take effect.
    time.sleep(5)

    run_after_connect("true", conn, wait=wait)


def run_after_connect(cmd, conn, wait=360):
    # override the Connection parameters
    orig_timeout = conn.connect_timeout
    conn.connect_timeout = 60
    timeout = time.time() + wait
    latest_exception = None

    try:
        while time.time() < timeout:
            try:
                print("will try to connect to host", conn.host)
                result = conn.run(cmd, hide=True)
                return result.stdout
            except ConnectionError as e:
                latest_exception = e
                print(
                    "Got SSH exception while connecting to host %s: %s" % (conn.host, e)
                )
                time.sleep(30)
                continue
            except Exception as e:
                print(
                    "Generic exception happened while connecting to host %s: %s"
                    % (conn.host, e)
                )
                print(type(e))
                print(e.args)
                raise e
    finally:
        # Restore the original connection parameters
        conn.connect_timeout = orig_timeout

    raise latest_exception


def determine_active_passive_part(bitbake_variables, conn):
    """Given the output from mount, determine the currently active and passive
    partitions, returning them as a pair in that order."""

    mount_output = conn.run("mount").stdout
    a = bitbake_variables["MENDER_ROOTFS_PART_A"]
    b = bitbake_variables["MENDER_ROOTFS_PART_B"]

    if mount_output.find(a) >= 0:
        return (a, b)
    elif mount_output.find(b) >= 0:
        return (b, a)
    else:
        raise Exception(
            "Could not determine active partition. Mount output:\n {}"
            "\nwas looking for {}".format(mount_output, (a, b))
        )


def get_ssh_common_args(conn):
    args = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    if "key_filename" in conn.connect_kwargs.keys():
        args += " -i %s" % conn.connect_kwargs["key_filename"]
    return args


# Yocto build SSH is lacking SFTP, let's override and use regular SCP instead.
def put_no_sftp(file, conn, remote="."):
    cmd = "scp -O %s" % get_ssh_common_args(conn)
    conn.local(
        "%s -P %s %s %s@%s:%s" % (cmd, conn.port, file, conn.user, conn.host, remote)
    )


# Yocto build SSH is lacking SFTP, let's override and use regular SCP instead.
def get_no_sftp(file, conn, local="."):
    cmd = "scp -O %s" % get_ssh_common_args(conn)
    conn.local(
        "%s -P %s %s@%s:%s %s" % (cmd, conn.port, conn.user, conn.host, file, local)
    )


def manual_uboot_commit(conn):
    _, bootenv_set = bootenv_tools(conn)
    conn.run(f"{bootenv_set} upgrade_available 0")
    conn.run(f"{bootenv_set} bootcount 0")


def latest_build_artifact(request, builddir, extension, sdimg_location=None):

    # Force the builddir to be an absolute path
    builddir = os.path.abspath(builddir)

    if request.config.getoption("--test-conversion"):
        output = subprocess.check_output(
            [
                "sh",
                "-c",
                "ls -t %s/*%s | grep -v data*%s| head -n 1"
                % (builddir, extension, extension),
            ]
        )
    else:
        output = subprocess.check_output(
            [
                "sh",
                "-c",
                "ls -t %s/tmp*/deploy/images/*/*%s | grep -v data*%s| head -n 1"
                % (builddir, extension, extension),
            ]
        )
    output = output.decode().rstrip("\r\n")
    print("Found latest image of type '%s' to be: %s" % (extension, output))
    return output


def get_bitbake_variables(request, target, prepared_test_build, export_only=False):
    lines = []

    if request.config.getoption("--test-conversion"):
        config_file_path = os.path.abspath(request.config.getoption("--test-variables"))
        with open(config_file_path, "r") as config:
            lines = config.readlines()
    else:
        current_dir = os.open(".", os.O_RDONLY)
        os.chdir(os.environ["BUILDDIR"])
        if prepared_test_build is not None:
            env_setup = "cd %s && . oe-init-build-env %s &&" % (
                prepared_test_build["bitbake_corebase"],
                prepared_test_build["build_dir"],
            )
        else:
            env_setup = "flock bitbake.test.lock"
        ps = subprocess.Popen(
            "%s bitbake -e %s" % (env_setup, target),
            stdout=subprocess.PIPE,
            shell=True,
            executable="/bin/bash",
        )
        os.fchdir(current_dir)

        while True:
            line = ps.stdout.readline()
            if not line:
                break
            lines.append(line.decode())

        ps.wait()

    if export_only:
        export_only_expr = ""
    else:
        export_only_expr = "?"
    matcher = re.compile('^(?:export )%s([A-Za-z][^=]*)="(.*)"$' % export_only_expr)
    ret = {}
    for line in lines:
        line = line.strip()
        match = matcher.match(line)
        if match is not None:
            ret[match.group(1)] = match.group(2)

    # For some unknown reason, 'MACHINE' is not included in the 'bitbake -e' output.
    # We set MENDER_MACHINE in mender-setup.bbclass as a proxy so look for that instead.
    if ret.get("MACHINE") is None:
        if ret.get("MENDER_MACHINE") is not None:
            ret["MACHINE"] = ret.get("MENDER_MACHINE")
        else:
            raise Exception("Could not determine MACHINE or MENDER_MACHINE value.")

    return ret


def signing_key(key_type):
    # RSA pregenerated using these.
    #   openssl genrsa -out files/test-private-RSA.pem 2048
    #   openssl rsa -in files/test-private-RSA.pem -outform PEM -pubout -out files/test-public-RSA.pem

    # EC pregenerated using these.
    #   openssl ecparam -genkey -name prime256v1 -out /tmp/private-and-params.pem
    #   openssl ec -in /tmp/private-and-params.pem -out files/test-private-EC.pem
    #   openssl ec -in files/test-private-EC.pem -pubout -out files/test-public-EC.pem

    class KeyPair:
        if key_type == "EC":
            private = "files/test-private-EC.pem"
            public = "files/test-public-EC.pem"
        else:
            private = "files/test-private-RSA.pem"
            public = "files/test-public-RSA.pem"

    return KeyPair()


# `capture` can be a bool, meaning the captured output is returned, or a stream,
# in which case the output is redirected there, and the process handle is
# returned instead.
def run_verbose(cmd, capture=False):
    if type(capture) is not bool:
        print('subprocess.Popen("%s")' % cmd)
        return subprocess.Popen(
            cmd,
            shell=True,
            executable="/bin/bash",
            stderr=subprocess.STDOUT,
            stdout=capture,
        )
    elif capture:
        print('subprocess.check_output("%s")' % cmd)
        return subprocess.check_output(
            cmd, shell=True, executable="/bin/bash", stderr=subprocess.STDOUT
        )
    else:
        print(cmd)
        return subprocess.check_call(cmd, shell=True, executable="/bin/bash")


# Capture is true or false and conditionally returns output.
def build_image(
    build_dir,
    bitbake_corebase,
    bitbake_image,
    extra_conf_params=None,
    extra_bblayers=None,
    target=None,
    capture=False,
):
    for param in extra_conf_params or []:
        _add_to_local_conf(build_dir, param)

    for layer in extra_bblayers or []:
        _add_to_bblayers_conf(build_dir, layer)

    init_env_cmd = "cd %s && . oe-init-build-env %s" % (bitbake_corebase, build_dir)

    if target:
        _run_bitbake(target, init_env_cmd, capture)
    else:
        _run_bitbake(bitbake_image, init_env_cmd, capture)


def _run_bitbake(target, env_setup_cmd, capture=False):
    cmd = "%s && bitbake %s" % (env_setup_cmd, target)
    ps = run_verbose(cmd, capture=subprocess.PIPE)
    output = ""
    try:
        # Cannot use for loop here due to buffering and iterators.
        while True:
            line = ps.stdout.readline().decode()
            if not line:
                break

            if line.find("is not a recognized MENDER_ variable") >= 0:
                pytest.fail(
                    "Found variable which is not in mender-vars.json: %s" % line.strip()
                )

            if capture:
                output += line
            else:
                sys.stdout.write(line)
    finally:
        # Empty any remaining lines.
        try:
            if capture:
                output += ps.stdout.readlines().decode()
            else:
                ps.stdout.readlines()
        except:
            pass
        ps.wait()
        if ps.returncode != 0:
            e = subprocess.CalledProcessError(ps.returncode, cmd)
            if capture:
                e.output = output
            raise e

    return output


# Make sure we are constructing the paths the same way always
def get_local_conf_path(build_dir):
    return os.path.join(build_dir, "conf", "local.conf")


def get_local_conf_orig_path(build_dir):
    return os.path.join(build_dir, "conf", "local.conf.orig")


def get_bblayers_conf_path(build_dir):
    return os.path.join(build_dir, "conf", "bblayers.conf")


def get_bblayers_conf_orig_path(build_dir):
    return os.path.join(build_dir, "conf", "bblayers.conf.orig")


def _add_to_local_conf(build_dir, string):
    """Add given string to local.conf before the build. Newline is added
    automatically."""
    with open(os.path.join(build_dir, "conf", "local.conf"), "a") as fd:
        fd.write("\n## ADDED BY TEST\n")
        fd.write("%s\n" % string)


def _add_to_bblayers_conf(build_dir, string):
    """Add given string to bblayers.conf before the build. Newline is added
    automatically."""
    with open(os.path.join(build_dir, "conf", "bblayers.conf"), "a") as fd:
        fd.write("\n## ADDED BY TEST\n")
        fd.write("%s\n" % string)


def reset_build_conf(build_dir, full_cleanup=False):
    # Restore original build configuration
    for conf in ["local", "bblayers"]:
        new_file = os.path.join(build_dir, "conf", conf + ".conf")
        old_file = new_file + ".orig"

        if os.path.exists(old_file):
            run_verbose("cp %s %s" % (old_file, new_file))
            if full_cleanup:
                os.remove(old_file)


class bitbake_env_from:
    old_env = {}
    old_path = None
    recipe = None
    prepared_test_build = None

    def __init__(self, request, recipe, prepared_test_build):
        self.recipe = recipe
        self.request = request
        self.prepared_test_build = prepared_test_build

    def __enter__(self):
        self.setup()

    def setup(self):
        if isinstance(self.recipe, str):
            vars = get_bitbake_variables(
                self.request, self.recipe, self.prepared_test_build, export_only=True
            )
        else:
            vars = self.recipe

        self.old_env = {}
        # Save all values that have keys in the bitbake_env_dict
        for key in vars:
            if key in os.environ:
                self.old_env[key] = os.environ[key]
            else:
                self.old_env[key] = None

        self.old_path = os.environ["PATH"]

        os.environ.update(vars)
        # Exception for PATH, keep old path at end.
        os.environ["PATH"] += ":" + self.old_path

        return os.environ

    def __exit__(self, type, value, traceback):
        self.teardown()

    def teardown(self):
        # Restore all keys we saved.
        for key in self.old_env:
            if self.old_env[key] is None:
                del os.environ[key]
            else:
                os.environ[key] = self.old_env[key]


def versions_of_recipe(recipe):
    """Returns a list of all the versions we have of the given recipe, excluding
    git recipes."""

    versions = []
    for entry in os.listdir("../../meta-mender-core/recipes-mender/%s/" % recipe):
        match = re.match(r"^%s_([1-9][0-9]*\.[0-9]+\.[0-9]+[^.]*)\.bb" % recipe, entry)
        if match is not None:
            versions.append(match.group(1))
    return versions


def version_is_minimum(bitbake_variables, component, min_version):
    version = bitbake_variables.get("PREFERRED_VERSION_pn-%s" % component)
    if version is None:
        version = bitbake_variables.get("PREFERRED_VERSION_%s" % component)
    if version is None:
        version = "master"

    try:
        if LooseVersion(min_version) > LooseVersion(version):
            return False
        else:
            return True
    except TypeError:
        # Type error indicates that 'version' is likely a string (branch
        # name). For now we just default to always consider them higher than the
        # minimum version.
        return True


@contextmanager
def make_tempdir(delete=True):
    """context manager for temporary directories"""
    tdir = tempfile.mkdtemp(prefix="meta-mender-acceptance.")
    print("created dir", tdir)
    try:
        yield tdir
    finally:
        if delete:
            shutil.rmtree(tdir)


MENDER_STATE_FILES = (
    "/var/lib/mender/mender-agent.pem",
    "/var/lib/mender/mender-store",
    "/var/lib/mender/mender-store-lock",
)


def cleanup_mender_state(connection):
    connection.run("rm -f %s" % " ".join(MENDER_STATE_FILES))


def bootenv_tools(connection):
    """Returns a tuple containing the print and set tools of the current bootloader."""

    result = connection.run("test -x /usr/bin/grub-mender-grubenv-print", warn=True)
    if result.return_code == 0:
        return ("grub-mender-grubenv-print", "grub-mender-grubenv-set")
    else:
        return ("fw_printenv", "fw_setenv")


def extract_partition(img, number, dst):
    output = subprocess.Popen(
        ["fdisk", "-l", "-o", "device,start,end", img], stdout=subprocess.PIPE
    )
    start = None
    end = None
    for line in output.stdout:
        if re.search("img%d" % number, line.decode()) is None:
            continue

        match = re.match(r"\s*\S+\s+(\S+)\s+(\S+)", line.decode())
        assert match is not None
        start = int(match.group(1))
        end = int(match.group(2)) + 1
    output.wait()

    assert start is not None
    assert end is not None
    subprocess.check_call(
        [
            "dd",
            "if=" + img,
            f"of={dst}/img{number}.fs",
            "skip=%d" % start,
            "count=%d" % (end - start),
        ]
    )
