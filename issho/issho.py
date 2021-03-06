# -*- coding: utf-8 -*-
"""
Implementation for the ``Issho`` class, which implements
a connection and some simple commands over ``ssh``, using
``keyring`` to manage secrets locally.
"""
import re
import sys
import time
from functools import partial
from shutil import copyfile

import humanize
import keyring
import paramiko
from sshtunnel import SSHTunnelForwarder

from issho.config import read_issho_conf
from issho.config import read_ssh_profile
from issho.helpers import add_arguments_to_cmd
from issho.helpers import clean_spark_options
from issho.helpers import default_sftp_path
from issho.helpers import get_pkey
from issho.helpers import get_user
from issho.helpers import issho_pw_name


class Issho:
    def __init__(self, profile="dev", kinit=True):
        self.local_user = get_user()
        self.profile = profile
        self.issho_conf = read_issho_conf(profile)
        self.ssh_conf = read_ssh_profile(self.issho_conf["SSH_CONFIG_PATH"], profile)
        self.hostname = self.ssh_conf.get("hostname", None)
        self.user = self.ssh_conf.get("user", None)
        self.port = self.ssh_conf.get("port", 22)
        self._ssh = self._connect()
        if kinit:
            self.kinit()
        self._remote_home_dir = self.get_output("echo $HOME").strip()
        return

    def __getattr__(self, method_name):
        """
        Allows the automatic creation of syntactic sugar methods like
        Issho.ls, Issho.mv, etc.
        :param method_name: the name of the uninstantiated method to be called
        :return: a partially-applied method
        """
        return partial(self.exec, method_name.replace("_", " "))

    def local_forward(
        self, remote_host, remote_port, local_host="0.0.0.0", local_port=44556
    ):
        """
        Forwards a port from a remote through this Issho object.
        Useful for connecting to remote hosts that can only be accessed
        from inside a VPC of which your devbox is part.
        """
        tunnel = SSHTunnelForwarder(
            (self.hostname, self.port),
            ssh_username=self.user,
            ssh_pkey=get_pkey(self.issho_conf["ID_RSA"]),
            remote_bind_address=(remote_host, remote_port),
            local_bind_address=(local_host, local_port),
        )
        tunnel.start()
        return tunnel

    def exec(self, cmd, *args, bg=False, debug=False, capture_output=False):
        """
        Execute a command in bash over the SSH connection.

        Note, this command does not use an interactive terminal;
        it instead uses a *non-interactive login* shell.
        This means (specifically) that your aliased commands will not work
        and only variables exported in your remote ``.bashrc`` will be available.

        :param cmd: The bash command to be run remotely

        :param *args: Additional arguments to the command cmd

        :param bg: True = run in the background

        :param debug: True = print some debugging output

        :param capture_output: True = return stdout as a string

        :return:
        """
        cmd = add_arguments_to_cmd(cmd, *args)
        if bg:
            cmd = 'cmd=$"{}"; nohup bash -c "$cmd" &'.format(cmd.replace('"', r"\""))
        if debug:
            print(args)
            print(cmd)
        stdin, stdout, stderr = self._ssh.exec_command(cmd)

        captured_output = ""
        for line in stdout:
            if capture_output:
                captured_output += line
            else:
                print(line, end="")

        for line in stderr:
            sys.stderr.write(line)
        return captured_output

    def exec_bg(self, cmd, *args, **kwargs):
        """
        Syntactic sugar for ``exec(bg=True)``
        """
        return self.exec(cmd, *args, bg=True, **kwargs)

    def get_output(self, cmd, *args, **kwargs):
        """
        Syntactic sugar for ``exec(capture_output=True)``
        """
        return self.exec(cmd, *args, **kwargs, capture_output=True)

    def get(self, remotepath, localpath=None, hadoop=False):
        """
        Gets the file at the remote path and puts it locally.

        :param remotepath: The path on the remote from which to get.

        :param localpath: Defaults to the name of the remote path

        :param hadoop: Download from HDFS
        """
        hadoop = hadoop or remotepath.startswith("hdfs")
        paths = self._sftp_paths(localpath=localpath, remotepath=remotepath)
        if hadoop:
            tmp_path = "/tmp/{}_{}".format(
                paths["localpath"].replace("/", "_"), time.time()
            )
            self.hadoop("get -f", remotepath, tmp_path)
            paths["remotepath"] = tmp_path
        with self._ssh.open_sftp() as sftp:
            sftp.get(
                remotepath=paths["remotepath"],
                localpath=paths["localpath"],
                callback=self._sftp_progress,
            )
        if hadoop:
            self.exec("rm", paths["remotepath"])
        return

    def put(self, localpath, remotepath=None, hadoop=False):
        """
        Puts the file at the local path to the remote.

        :param localpath: The local path of the file to put to the remote

        :param remotepath: Defaults to the name of the local path

        :param hadoop: Upload to HDFS
        """
        hadoop = hadoop or remotepath.startswith("hdfs")
        paths = self._sftp_paths(localpath=localpath, remotepath=remotepath)
        if hadoop:
            tmp_path = "/tmp/{}_{}".format(localpath.replace("/", "_"), time.time())
            paths["remotepath"] = tmp_path
        with self._ssh.open_sftp() as sftp:
            sftp.put(
                localpath=paths["localpath"],
                remotepath=paths["remotepath"],
                callback=self._sftp_progress,
            )
        if hadoop:
            self.hadoop("put", paths["remotepath"], remotepath)
            self.exec("rm", paths["remotepath"])
        return

    def kinit(self):
        """
        Runs kerberos init
        """
        kinit_pw = self._get_password("kinit")
        if kinit_pw:
            self.exec("echo {} | kinit".format(kinit_pw))
        else:
            raise OSError(
                "Add your kinit password with `issho config <profile>` "
                "or by editing `~/.issho/config.toml`"
            )
        return

    def hive(self, query, output_filename=None, remove_blank_top_line=True):
        """
        Runs a hive query using the parameters
        set in .issho/config.toml

        :param query: a string query, or the name of a query file
            name to run.
        :param output_filename: the (local) file to output the results
            of the hive query to. Adding this option will also
            keep a copy of the results in /tmp
        :param remove_blank_top_line: Hive usually has a blank top line
            when data is output, this parameter removes it.
        """
        query = str(query)
        tmp_filename = "/tmp/issho_{}.sql".format(time.time())
        if query.endswith("sql") or query.endswith("hql"):
            copyfile(query, tmp_filename)
        else:
            with open(tmp_filename, "w") as f:
                f.write(query)
        self.put(tmp_filename, tmp_filename)

        tmp_output_filename = "{}.output".format(tmp_filename)

        hive_cmd_template = """
        beeline {opts} -u  "{jdbc}" -f {fn} {remove_first_line} {redirect_to_tmp_fn}
        """.strip()

        hive_cmd = hive_cmd_template.format(
            opts=self.issho_conf["HIVE_OPTS"],
            jdbc=self.issho_conf["HIVE_JDBC"],
            fn=tmp_filename,
            remove_first_line="| sed 1d" if remove_blank_top_line else "",
            redirect_to_tmp_fn="> {}".format(tmp_output_filename)
            if output_filename
            else "",
        )

        self.exec(hive_cmd)

        if output_filename:
            self.get(tmp_output_filename, output_filename)

    def spark_submit(
        self,
        spark_options=None,
        master="",
        jars="",
        files="",
        driver_class_path="",
        application_class="",
        application="",
        application_args="",
    ):
        """
        Submit a spark job.

        :param spark_options: A dict of spark options
        :param master: syntactic sugar for the --master spark option
        :param jars: syntactic sugar for the --jars spark option
        :param files: syntactic sugar for the --files spark option
        :param driver_class_path: syntactic sugar for the --driver-class-path spark option
        :param application_class: syntactic sugar for the --class spark option
        :param application: the application to submit
        :param application_args: any arguments to be passed to the spark application
        :return:
        """
        assert application
        if not spark_options:
            spark_options = {}
        for k, v in locals().items():
            if k in {
                "spark_options",
                "application",
                "application_args",
                "self",
                "bg",
                "debug",
            }:
                continue
            clean_keys = {"application_class": "class"}
            clean_k = clean_keys.get(k, k)
            if v:
                spark_options[clean_k] = v

        cleaned_spark_options = clean_spark_options(spark_options)
        spark_options_str = " ".join(
            (
                "{} {}".format(k, v)
                for k, v in sorted(cleaned_spark_options.items(), key=lambda x: x[0])
            )
        )
        spark_cmd = "spark-submit {} {} {}".format(
            spark_options_str, application, application_args
        )
        self.exec(spark_cmd)

    def spark(self, *args, **kwargs):
        """
        Syntactic sugar for spark_submit
        """
        self.spark_submit(*args, **kwargs)

    def hadoop(self, command, *args, **kwargs):
        """
        Execute the hadoop command
        :param command:
        :param args:
        :param kwargs:
        :return:
        """
        hadoop_cmd = "-{}".format(re.sub("^-*", "", command))
        return self.exec("hadoop fs", hadoop_cmd, *args, **kwargs)

    def hdfs(self, *args, **kwargs):
        """
        Syntactic sugar for hadoop
        """
        return self.hadoop(*args, **kwargs)

    def _connect(self):
        """
        Uses paramiko to connect to the remote specified
        :return:
        """
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            self.hostname,
            username=self.user,
            port=self.port,
            pkey=get_pkey(self.issho_conf["RSA_ID_PATH"]),
        )
        return ssh

    def _sftp_paths(self, localpath, remotepath):
        localpath = default_sftp_path(localpath, remotepath)
        remotepath = default_sftp_path(remotepath, localpath)
        return {
            "localpath": str(localpath.expanduser()),
            "remotepath": str(remotepath).replace("~", self._remote_home_dir),
        }

    @staticmethod
    def _sftp_progress(transferred, to_transfer):
        print(
            "{} transferred out of a total of {}".format(
                humanize.naturalsize(transferred), humanize.naturalsize(to_transfer)
            )
        )

    def _get_password(self, pw_type):
        return keyring.get_password(
            issho_pw_name(pw_type=pw_type, profile=self.profile), self.local_user
        )
