#!/usr/bin/env python3
# encoding: utf-8

import lxc
import sys
import logging
import argparse
import socket
import os
import inspect
import shutil
import pprint
import locale
import json
import tempfile
import time
import stat
import atexit
import platform
import pwd
import collections


INET_IFACE_NAME = "inet0"
INET_BRIDGE_NAME = "lxcbr0"



__programm__ = "vhost-manager"
__version__  = "1"

pp = pprint.PrettyPrinter(indent=4)

TMPDIR = tempfile.mkdtemp()

class ConfigurationException(Exception): pass
class ArgumentException(Exception): pass
class EnvironmentException(Exception): pass
class InternalException(Exception): pass


class TopologyDb(object):

    def __init__(self, connections, directed=False):
        self._graph = collections.defaultdict(set)
        self._directed = directed
        self.add_connections(connections)

    def gen_digraph(self):
        d  =  "digraph foo { node [ fontname = \"DejaVu Sans\" ];"
        d += " edge [ fontname = \"DejaVu Sans\" ];\n"
        for k, v in self._graph.items():
            for v2 in v:
                ks = k.graphviz_repr()
                vs = v2.graphviz_repr()
                d += "\"{}\" -> \"{}\" [ arrowhead = \"none\", arrowtail = \"normal\"];\n".format(ks, vs)
        d += "}\n"
        return d

    def get_bridges(self):
        ret = []
        for k, v in self._graph.items():
            for v2 in v:
                if v2 is not None and isinstance(v2, Bridge):
                    ret.append(v2)
            if k is not None and isinstance(k, Bridge):
                ret.append(k)
        return ret

    def get_hosts(self):
        ret = []
        for k, v in self._graph.items():
            for v2 in v:
                if v2 is not None and isinstance(v2, Host):
                    ret.append(v2)
            if k is not None and isinstance(k, Host):
                ret.append(k)
        return ret


    def add_connections(self, connections):
        if not connections: return
        for node1, node2 in connections:
            self.add(node1, node2)

    def add(self, node1, node2):
        self._graph[node1].add(node2)
        if not self._directed:
            self._graph[node2].add(node1)

    def remove(self, node):
        for n, cxns in self._graph.iteritems():
            try:
                cxns.remove(node)
            except KeyError:
                pass
        try:
            del self._graph[node]
        except KeyError:
            pass

    def is_connected(self, node1, node2):
        return node1 in self._graph and node2 in self._graph[node1]

    def find_path(self, node1, node2, path=[]):
        path = path + [node1]
        if node1 == node2:
            return path
        if node1 not in self._graph:
            return None
        for node in self._graph[node1]:
            if node not in path:
                new_path = self.find_path(node, node2, path)
                if new_path:
                    return new_path
        return None

    def __str__(self):
        return '{}({})'.format(self.__class__.__name__, dict(self._graph))


class Host:

    def __init__(self, name, p, u, c, h):
        self.u = u
        self.c = c
        self.p = p
        self.name = name
        self.config = h
        self.container = None
        self.init_user_credentials()

    def __str__(self):
        return "{}".format(self.name)

    def init_user_credentials(self):
        userdata = self.c.db["user"]
        self.username = userdata["username"]
        self.userpass = userdata["userpass"]

    def remove_tmp_files(self):
        close(self.tf_lxc)
        close(self.tf_net)

    def tmp_file_new(self, string):
        name = os.path.join(TMPDIR, string)
        fd = open(name,"w")
        return fd, name

    def tmp_file_destroy(self, name):
        os.remove(name)

    def create_container(self):
        fd, name = self.tmp_file_new("lxc-conf")
        config = self.config['config']['conf-lxc']
        fd.write(config)
        os.fsync(fd); fd.close()

        # sudo LC_ALL=C lxc-create --bdev dir -f $(dirname "${BASH_SOURCE[0]}")/lxc-config
        # -n $name -t $distribution --logpriority=DEBUG --logfile $logpath -- -r xenial")
        # FIXME: logging should be activatable
        cmd  = "sudo LC_ALL=C lxc-create --bdev dir -n {} ".format(self.name)
        cmd += "-f {} -t ubuntu -- -r xenial".format(name)
        self.u.exec(cmd)
        self.tmp_file_destroy(name)

    def start_container(self):
        self.u.exec("sudo lxc-start -n {} -d".format(self.name))
        self.container = lxc.Container(self.name)

    def stop_container(self):
        self.u.exec("sudo lxc-stop -n {}".format(self.name))

    def restart_container(self):
        self.stop_container()
        self.start_container()

    def exec(self, cmd, user=None):
        if user:
            cmd = "lxc-attach -n {} --clear-env -- bash -c \"su - {} -c \'{}\'\"".format(self.name, user, cmd)
        else:
            cmd = "lxc-attach -n {} --clear-env -- bash -c \"{}\"".format(self.name, cmd)
        self.u.exec(cmd)

    def container_file_copy(self, name, src_path, dst_path, user=None):
        cmd  = "cat {} | lxc-attach -n {} ".format(src_path, name)
        cmd += " --clear-env -- bash -c 'cat >{}'".format(dst_path)
        self.u.exec(cmd)
        # we don't want a race here: we don't know when the new process
        # is scheduled, so we sleep here for a short period, just to make sure[TM]
        # that the new process is executed.
        time.sleep(.5)
        if user:
            self.exec("chown -R {}:{} {}".format(user, user, dst_path))

    def copy_interface_conf(self):
        tmp_fd, tmp_name = self.tmp_file_new("lxc-conf")
        config = self.config['config']['conf-debian-interface']
        tmp_fd.write(config)
        os.fsync(tmp_fd); tmp_fd.close()
        self.container_file_copy(self.name, tmp_name, "/etc/network/interfaces")
        self.tmp_file_destroy(tmp_name)

    def create_user_account(self):
        self.exec("useradd --create-home --shell /bin/bash --user-group {}".format(self.username))
        self.exec("echo '{}:{}' | chpasswd".format(self.username, self.userpass))
        self.exec("echo '{} ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers".format(self.username))

    def user_home_dir(self):
        # this function handles also SUDO invoked calls
        if "SUDO_UID" not in os.environ:
            path = pwd.getpwuid(os.getresuid()[0])[5]
        else:
            path = pwd.getpwuid(int(os.getenv("SUDO_UID")))[5]
        return path

    def copy_dotfiles_plain(self, assets_dir):
        vimrc_path = os.path.join(assets_dir, "vimrc")
        dst_home_path = os.path.join("/home", self.username)
        assert os.path.isfile(vimrc_path)
        dst_path = os.path.join(dst_home_path, ".vimrc")
        self.container_file_copy(self.name, vimrc_path, dst_path, user=self.username)

        # bashrc, if user has local one we prefer this one (e.g. proxy settings)
        # note: we assume here the user is using sudo, the real user home path
        effective_home_path = self.user_home_dir()
        bashrc_path = os.path.join(effective_home_path, ".bashrc")
        dst_path = os.path.join(dst_home_path, ".bashrc")
        if not os.path.isfile(bashrc_path):
            # take own provided bashrc
            bashrc_path = os.path.join(assets_dir, "bashrc")
        self.container_file_copy(self.name, bashrc_path, dst_path, user=self.username)

    def copy_dotfiles(self):
        root_dir = os.path.dirname(os.path.realpath(__file__))
        assets_dir = os.path.join(root_dir, "assets")
        self.copy_dotfiles_plain(assets_dir)

    def copy_distribution_specific(self):
        distribution = platform.linux_distribution()
        if distribution[0] != "Ubuntu":
            return
        # apt.conf contains proxy settings
        filepath = "/etc/apt/apt.conf"
        if os.path.isfile(filepath):
            self.container_file_copy(self.name, filepath, filepath)

    def install_base_packages(self):
        self.exec("apt-get -y update")
        self.exec("apt-get -y install git vim bash python3")

    def bootstrap_packages(self):
        self.exec("git clone https://github.com/hgn/tr-bootstrapper.git", user=self.username)
        self.exec("python3 tr-bootstrapper/bootstrap.py -vvv", user=self.username)

    def create(self):
        self.p.msg("Create container: {}\n".format(self), stoptime=1.0)
        self.create_container()
        self.start_container()
        self.copy_interface_conf()
        self.restart_container()
        self.create_user_account()
        self.copy_dotfiles()
        self.copy_distribution_specific()
        self.install_base_packages()
        self.bootstrap_packages()
        self.stop_container()

class Terminal(Host):

    def graphviz_repr(self):
        return 'Terminal({})'.format(self.name)

    def __str__(self):
        return "Terminal({})".format(self.name)


class Router(Host):

    def graphviz_repr(self):
        return 'Router({})'.format(self.name)

    def __str__(self):
        return "Router({})".format(self.name)


class UE(Host):

    def graphviz_repr(self):
        return 'UE({})'.format(self.name)

    def __str__(self):
        return "UE({})".format(self.name)


class Bridge:

    def __init__(self, name, p, u, c, h):
        self.u = u
        self.p = p
        self.name = name

    def __str__(self):
        return "Bridge({})".format(self.name)

    def create(self):
        self.p.msg("Create bridge: {}\n".format(self.name))
        brige_path = os.path.join("/sys/class/net", self.name)
        if os.path.isdir(brige_path):
            self.p.msg("bridge {} already created\n".format(self.name), color="red")
            return
        self.u.exec("brctl addbr {}".format(self.name))
        self.u.exec("brctl setfd {} 0".format(self.name))
        self.u.exec("brctl sethello {} 5".format(self.name))
        self.u.exec("ip link set dev {} up".format(self.name))

    def graphviz_repr(self):
        return 'Bridge({})'.format(self.name)


class Printer:

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.init_colors()

    def init_colors(self):
        self.color_palette = {
                'yellow':'\033[0;33;40m',
                'foo':'\033[93m',
                'red':'\033[91m',
                'green':'\033[92m',
                'blue':'\033[94m',
                'end':'\033[0m'
                }
        is_a_tty = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
        if not is_a_tty:
            for key, value in self.color_palette.items():
                self.color_palette[key] = ""

    def set_verbose(self):
        self.verbose = True

    def err(self, msg):
        sys.stderr.write(msg)

    def verbose(self, msg):
        if not self.verbose:
            return
        sys.stderr.write(msg)

    def msg(self, msg, stoptime=None, color="yellow", clear=False):
        if clear: self.clear()
        if color:
            if color in self.color_palette:
                msg = "{}{}{}".format(self.color_palette[color], msg, self.color_palette['end'])
            else:
                raise InternalException("Color not known")
        ret = sys.stdout.write(msg) - 1
        if stoptime:
            time.sleep(stoptime)
        return ret

    def line(self, length, char='-'):
        sys.stdout.write(char * length + "\n")

    def msg_underline(self, msg, pre_news=0, post_news=0):
        str_len = len(msg)
        if pre_news:
            self.msg("\n" * pre_news)
        self.msg(msg)
        self.msg("\n" + '=' * str_len)
        if post_news:
            self.msg("\n" * post_news)

    def clear(self):
        os.system("clear")


class Utils:

    def exec(self, args):
        print("execute: \"{}\"".format(args))
        os.system(args)
    
    def query_yes_no(self, question, default="yes"):
        valid = {"yes": True, "y": True, "ye": True,
                "no": False, "n": False}
        if default is None:
            prompt = " [y/n] "
        elif default == "yes":
            prompt = " [Y/n] "
        elif default == "no":
            prompt = " [y/N] "
        else:
            raise ValueError("invalid default answer: '%s'" % default)
        
        while True:
            sys.stdout.write(question + prompt)
            choice = input().lower()
            if default is not None and choice == '':
                return valid[default]
            elif choice in valid:
                return valid[choice]
            else:
                sys.stdout.write("Please respond with 'yes' or 'no' "
                                 "(or 'y' or 'n').\n")


class Configuration():

    def __init__(self, topology_name):
        self.db = self.load_configuration("conf.json")
        self.topology_name = topology_name

    def load_configuration(self, filename):
        with open(filename) as json_data:
            d = json.load(json_data)
            json_data.close()
            return d

    def is_valid(self):
        for topology in self.db["topologies"]:
            if self.topology_name == topology:
                return True
        raise ArgumentException("topology not found: {}".format(self.topology_name))

    def bridge_handle(self, bridge_name):
        return bridge_name

    def terminal_gen_config(self, terminal_data):
        d = {}
        # standard data always present
        e  = "auto lo\n"
        e += "iface lo inet loopback\n\n"
        e += "auto inet0\n"
        e += "iface inet0 inet dhcp\n\n"

        # Debian Network section
        for interface_name, interface_data in terminal_data["interfaces"].items():
            e += "auto {}\n".format(interface_name)
            e += "iface {} inet static\n".format(interface_name)
            e += "  address {}\n".format(interface_data["ipv4-addr"])
            e += "  netmask {}\n".format(interface_data["ipv4-addr-netmask"])
            if "post-up" in interface_data:
                assert isinstance(interface_data["post-up"], list)
                for line in interface_data["post-up"]:
                    e += "  post-up {}\n".format(line)
            e += "\n"
        d["conf-debian-interface"] = e

        # LXC section
        e  = "lxc.network.type = veth\n"
        e += "lxc.network.name = {}\n".format(INET_IFACE_NAME)
        e += "lxc.network.flags = up\n"
        e += "lxc.network.link = {}\n".format(INET_BRIDGE_NAME)
        e += "lxc.network.hwaddr = 00:11:xx:xx:xx:xx\n\n"
        
        for interface_name, interface_data in terminal_data["interfaces"].items():
            e += "lxc.network.type = veth\n"
            e += "lxc.network.name = {}\n".format(interface_name)
            e += "lxc.network.flags = up\n"
            e += "lxc.network.link = {}\n".format(interface_data["lxr-link"])
            e += "lxc.network.hwaddr = {}\n\n".format(interface_data["lxr-hw-addr"])
        d["conf-lxc"] = e
        return d

    def host_handle(self, name):
        d = dict()
        for i in ("terminals", "router", "ue"):
            if name in self.db["devices"][i]:
                terminal = self.db["devices"][i][name]
                d['config'] = self.terminal_gen_config(terminal)
                return d
        raise ConfigurationException("entity (router, terminal, ...) not found: {}".format(name))

    def create_entity_object(self, entry_type, entry_name, p, u, c):
        if entry_type == "Router":
            h = self.host_handle(entry_name)
            return Router(entry_name, p, u, c, h)
        if entry_type == "Terminal":
            h = self.host_handle(entry_name)
            return Terminal(entry_name, p, u, c, h)
        if entry_type == "UE":
            h = self.host_handle(entry_name)
            return UE(entry_name, p, u, c, h)
        if entry_type == "Bridge":
            h = self.bridge_handle(entry_name)
            return Bridge(entry_name, p, u, c, h)
        assert False
        

    def create_topology_db(self, topology_name, p, u, c):
        topo = self.db["topologies"][self.topology_name]
        g = TopologyDb(None, directed=True)
        for item in topo["map"]:
            entries = item.split()
            assert len(entries) == 3
            assert entries[1] == "<->"
            # src
            entity_pair = entries[0].split('(')
            entity_type = entity_pair[0]
            entity_name = entity_pair[1].split(')')[0]
            o1 = self.create_entity_object(entity_type, entity_name, p, u, c)

            # dst
            entity_pair = entries[2].split('(')
            entity_type = entity_pair[0]
            entity_name = entity_pair[1].split(')')[0]
            o2 = self.create_entity_object(entity_type, entity_name, p, u, c)
            g.add(o1, o2)
        return g



class BridgeCreator():

    def __init__(self, utils, printer, name):
        self.u = utils
        self.p = printer
        self.bridge_name = name

    def create(self):
        brige_path = os.path.join("/sys/class/net", self.bridge_name)
        if os.path.isdir(brige_path):
            self.p.msg("bridge {} already created\n".format(self.bridge_name))
            return
        self.u.exec("brctl addbr {}".format(self.bridge_name))
        self.u.exec("brctl setfd {} 0".format(self.bridge_name))
        self.u.exec("brctl sethello {} 5".format(self.bridge_name))
        self.u.exec("ip link set dev {} up".format(self.bridge_name))


class HostCreator():

    def __init__(self, utils, c, p, name, config):
        self.u = utils
        self.c = c
        self.p = p
        self.name = name
        self.config = config
        self.container = None
        self.init_user_credentials()

    def init_user_credentials(self):
        userdata = self.c.db["user"]
        self.username = userdata["username"]
        self.userpass = userdata["userpass"]

    def remove_tmp_files(self):
        close(self.tf_lxc)
        close(self.tf_net)

    def tmp_file_new(self, string):
        name = os.path.join(TMPDIR, string)
        fd = open(name,"w")
        return fd, name

    def tmp_file_destroy(self, name):
        os.remove(name)

    def create_container(self):
        fd, name = self.tmp_file_new("lxc-conf")
        config = self.config['config']['conf-lxc']
        fd.write(config)
        os.fsync(fd); fd.close()

        # sudo LC_ALL=C lxc-create --bdev dir -f $(dirname "${BASH_SOURCE[0]}")/lxc-config
        # -n $name -t $distribution --logpriority=DEBUG --logfile $logpath -- -r xenial")
        # FIXME: logging should be activatable
        cmd  = "sudo LC_ALL=C lxc-create --bdev dir -n {} ".format(self.name)
        cmd += "-f {} -t ubuntu -- -r xenial".format(name)
        self.u.exec(cmd)
        self.tmp_file_destroy(name)

    def start_container(self):
        self.u.exec("sudo lxc-start -n {} -d".format(self.name))
        self.container = lxc.Container(self.name)

    def stop_container(self):
        self.u.exec("sudo lxc-stop -n {}".format(self.name))

    def restart_container(self):
        self.stop_container()
        self.start_container()

    def exec(self, cmd, user=None):
        if user:
            cmd = "lxc-attach -n {} --clear-env -- bash -c \"su - {} -c \'{}\'\"".format(self.name, user, cmd)
        else:
            cmd = "lxc-attach -n {} --clear-env -- bash -c \"{}\"".format(self.name, cmd)
        self.u.exec(cmd)

    def container_file_copy(self, name, src_path, dst_path, user=None):
        cmd  = "cat {} | lxc-attach -n {} ".format(src_path, name)
        cmd += " --clear-env -- bash -c 'cat >{}'".format(dst_path)
        self.u.exec(cmd)
        # we don't want a race here: we don't know when the new process
        # is scheduled, so we sleep here for a short period, just to make sure[TM]
        # that the new process is executed.
        time.sleep(.5)
        if user:
            self.exec("chown -R {}:{} {}".format(user, user, dst_path))

    def copy_interface_conf(self):
        tmp_fd, tmp_name = self.tmp_file_new("lxc-conf")
        config = self.config['config']['conf-debian-interface']
        tmp_fd.write(config)
        os.fsync(tmp_fd); tmp_fd.close()
        self.container_file_copy(self.name, tmp_name, "/etc/network/interfaces")
        self.tmp_file_destroy(tmp_name)

    def create_user_account(self):
        self.exec("useradd --create-home --shell /bin/bash --user-group {}".format(self.username))
        self.exec("echo '{}:{}' | chpasswd".format(self.username, self.userpass))
        self.exec("echo '{} ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers".format(self.username))

    def user_home_dir(self):
        # this function handles also SUDO invoked calls
        if "SUDO_UID" not in os.environ:
            path = pwd.getpwuid(os.getresuid()[0])[5]
        else:
            path = pwd.getpwuid(int(os.getenv("SUDO_UID")))[5]
        return path

    def copy_dotfiles_plain(self, assets_dir):
        vimrc_path = os.path.join(assets_dir, "vimrc")
        dst_home_path = os.path.join("/home", self.username)
        assert os.path.isfile(vimrc_path)
        dst_path = os.path.join(dst_home_path, ".vimrc")
        self.container_file_copy(self.name, vimrc_path, dst_path, user=self.username)

        # bashrc, if user has local one we prefer this one (e.g. proxy settings)
        # note: we assume here the user is using sudo, the real user home path
        effective_home_path = self.user_home_dir()
        bashrc_path = os.path.join(effective_home_path, ".bashrc")
        dst_path = os.path.join(dst_home_path, ".bashrc")
        if not os.path.isfile(bashrc_path):
            # take own provided bashrc
            bashrc_path = os.path.join(assets_dir, "bashrc")
        self.container_file_copy(self.name, bashrc_path, dst_path, user=self.username)

    def copy_dotfiles(self):
        root_dir = os.path.dirname(os.path.realpath(__file__))
        assets_dir = os.path.join(root_dir, "assets")
        self.copy_dotfiles_plain(assets_dir)

    def copy_distribution_specific(self):
        distribution = platform.linux_distribution()
        if distribution[0] != "Ubuntu":
            return
        # apt.conf contains proxy settings
        filepath = "/etc/apt/apt.conf"
        if os.path.isfile(filepath):
            self.container_file_copy(self.name, filepath, filepath)

    def install_base_packages(self):
        self.exec("apt-get -y update")
        self.exec("apt-get -y install git vim bash python3")

    def bootstrap_packages(self):
        self.exec("git clone https://github.com/hgn/tr-bootstrapper.git", user=self.username)
        self.exec("python3 tr-bootstrapper/bootstrap.py -vvv", user=self.username)

    def create(self):
        self.create_container()
        self.start_container()
        self.copy_interface_conf()
        self.restart_container()
        self.create_user_account()
        self.copy_dotfiles()
        self.copy_distribution_specific()
        self.install_base_packages()
        self.bootstrap_packages()
        self.stop_container()


class Creator():

    def __init__(self):
        self.u = Utils()
        self.p = Printer()
        self.parse_local_options()

    def parse_local_options(self):
        parser = argparse.ArgumentParser()
        parser.add_argument( "-v", "--verbose", dest="verbose", default=False,
                          action="store_true", help="show verbose")
        parser.add_argument("topology", help="name of the topology", type=str)
        self.args = parser.parse_args(sys.argv[2:])
        if self.args.verbose:
            self.p.set_verbose()

    def destroy_existing_container(self, topology_db):
        for host in topology_db.get_hosts():
            c = lxc.Container(host.name)
            if c.defined:
                self.p.msg("Container {} already exists\n".format(host.name))
                question = "Delete container or exit?"
                answer = self.u.query_yes_no(question, default="no")
                if answer == True:
                    print("Delete container in 1 seconds ...")
                    time.sleep(1)
                    c.stop()
                    if not c.destroy():
                        print("Failed to destroy the container!")
                        sys.exit(1)
                else:
                    print("exiting, call \"sudo lxc-ls --fancy\" to see all container")
                    exit(1)

    def start_container(self, host):
        c = lxc.Container(host.name)
        if c.defined:
            c.start()

    def tmp_dig_fd_new(self, string):
        name = os.path.join(TMPDIR, string)
        fd = open(name,"w")
        return fd, name

    def run(self):
        try:
            self.c = Configuration(self.args.topology)
        except ArgumentException as e:
            print("not a valid topology: {}".format(e))
            sys.exit(1)

        topology_db = self.c.create_topology_db(self.args.topology, self.p, self.u, self.c)
        self.destroy_existing_container(topology_db)

        done = []
        for bridge in topology_db.get_bridges():
            if bridge.name in done:
                continue
            bridge.create()
            done.append(bridge.name)

        done = []
        for host in topology_db.get_hosts():
            if host.name in done: continue
            host.create()
            done.append(host.name)

        done = []
        for host in topology_db.get_hosts():
            if host.name in done: continue
            self.p.msg("Start container: {}".format(host.name))
            self.start_container(host)
            done.append(host.name)



class Graphor():

    def __init__(self):
        self.u = Utils()
        self.p = Printer()
        self.parse_local_options()

    def parse_local_options(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("topology", help="name of the topology", type=str)
        self.args = parser.parse_args(sys.argv[2:])

    def start_container(self, host):
        c = lxc.Container(host.name)
        if c.defined:
            c.start()

    def tmp_dig_fd_new(self, string):
        name = os.path.join(TMPDIR, string)
        fd = open(name, "w")
        return fd, name

    def gen_digraph_image(self, topology_db):
        d = topology_db.gen_digraph()
        fd, name = self.tmp_dig_fd_new("digraph.data")
        fd.write(d)
        os.fsync(fd); fd.close()
        os.system("cat {} | dot -Tpng > graph.png".format(name))
        self.p.msg("file: graph.png\n")

    def run(self):
        try:
            self.c = Configuration(self.args.topology)
        except ArgumentException as e:
            self.p.msg("Not a valid topology: {}".format(e))
            sys.exit(1)

        topology_db = self.c.create_topology_db(self.args.topology, self.p, self.u, self.c)
        self.p.msg("Generate graph of topopolgy\n")
        self.gen_digraph_image(topology_db)


class Lister():

    def __init__(self):
        self.p = Printer()
        self.parse_local_options()


    def parse_local_options(self):
        parser = argparse.ArgumentParser()
        parser.add_argument( "-v", "--verbose", dest="verbose", default=False,
                          action="store_true", help="show verbose")
        self.args = parser.parse_args(sys.argv[2:])
        if self.args.verbose:
            self.p.set_verbose()

    def run(self):
        print("Available container: ")
        for container in lxc.list_containers(as_object=True):
            print(container.name)
            if not container.running:
                print("\tnot running ")
            else:
                print("\t    running ")




class VHostManager:

    modes = {
       "create": [ "Creator", "create given topolology, including bridge and container" ],
       "graph":  [ "Graphor", "create image of a given toplogy" ],
       "list":   [ "Lister",  "list available topologies" ]
            }

    def __init__(self):
        self.p = Printer()
        self.p.msg("Welcome!\n", color="yellow", clear=True)
        self.check_env()

    def install_packages_ubuntu(self):
        os.system("apt-get --yes --force-yes update")
        os.system("apt-get --yes --force-yes install lxc tmux ssh graphviz")
        return True

    def install_packages_arch(self):
        os.system("pacman -Syu --noconfirm")
        os.system("pacman -Sy --noconfirm ebtables community/lxc community/debootstrap community/tmux")
        return True

    def check_installed_packages(self):
        distribution = platform.linux_distribution()
        if distribution[0] == "Ubuntu":
            self.p.msg("seems you are using Ubuntu, great ...\n")
            self.install_packages_ubuntu()
        elif distribution[0] == "arch":
            self.p.msg("seems you are using Arch, great ...\n")
            self.install_packages_arch()
        else:
            raise EnvironmentException("Distribution not detected")

    def first_startup(self, touch_dir):
        touch_file = os.path.join(touch_dir, "already-started")
        if not os.path.isfile(touch_file):
            self.p.clear()
            self.p.msg("Seems you are new - great!\n", stoptime=1.0)
            self.p.clear()
            self.p.msg("I will make sure every required package is installed ...\n", stoptime=2.0)
            self.check_installed_packages()
            with open(touch_file, "w") as f:
                f.write("{}".format(time.time()))

    def check_ssh_keys(self, tmp_dir):
        ssh_file = os.path.join(tmp_dir, "ssh-id-rsa")
        if not os.path.isfile(ssh_file):
            self.p.clear()
            self.p.msg("No SSH key found! I will generate a new one ...\n", stoptime=2.0)
            os.system("ssh-keygen -f tmp/ssh-id-rsa -N ''")


    def check_env(self):
        script_dir = os.path.dirname(os.path.realpath(__file__))
        tmp_dir    = os.path.join(script_dir, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)

        self.first_startup(tmp_dir)
        self.check_ssh_keys(tmp_dir)

    def print_version(self):
        sys.stdout.write("%s\n" % (__version__))

    def print_usage(self):
        sys.stderr.write("Usage: vhost-manager [-h | --help]" +
                         " [--version]" +
                         " <submodule> [<submodule-options>]\n")

    def print_modules(self):
        for i in VHostManager.modes.keys():
            sys.stderr.write("   %-15s - %s\n" % (i, VHostManager.modes[i][1]))

    def args_contains(self, argv, *cmds):
        for cmd in cmds:
            for arg in argv:
                if arg == cmd: return True
        return False

    def parse_global_otions(self):
        if len(sys.argv) <= 1:
            self.print_usage()
            sys.stderr.write("Available submodules:\n")
            self.print_modules()
            return None

        self.binary_path = sys.argv[-1]

        # --version can be placed somewhere in the
        # command line and will evalutated always: it is
        # a global option
        if self.args_contains(sys.argv, "--version"):
            self.print_version()
            return None

        # -h | --help as first argument is treated special
        # and has other meaning as a submodule
        if self.args_contains(sys.argv[1:2], "-h", "--help"):
            self.print_usage()
            sys.stderr.write("Available modules:\n")
            self.print_modules()
            return None

        submodule = sys.argv[1].lower()
        if submodule not in VHostManager.modes:
            self.print_usage()
            sys.stderr.write("Modules \"%s\" not known, available modules are:\n" %
                             (submodule))
            self.print_modules()
            return None

        classname = VHostManager.modes[submodule][0]
        return classname


    def run(self):
        classtring = self.parse_global_otions()
        if not classtring:
            return 1

        classinstance = globals()[classtring]()
        classinstance.run()
        return 0

def remove_tmp_dir():
    shutil.rmtree(TMPDIR, ignore_errors=True)

if __name__ == "__main__":
    try:
        atexit.register(remove_tmp_dir)
        euid = os.geteuid() 
        if euid != 0:
            print("Need to be root")
            print("➡ sudo {}".format(sys.argv[0]))
            exit(1)

        vhm = VHostManager()
        sys.exit(vhm.run())
    except KeyboardInterrupt:
        sys.stderr.write("SIGINT received, exiting\n")
