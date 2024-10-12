#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.
import importlib.util
import importlib.machinery
import os
import sys
import traceback

from gunicorn import util
from gunicorn.arbiter import Arbiter
from gunicorn.config import Config, get_default_config_file
from gunicorn import debug


class BaseApplication:
    """
    An application interface for configuring and loading
    the various necessities for any given web framework.
    """
    def __init__(self, usage=None, prog=None):
        self.usage = usage
        self.cfg = None
        self.callable = None
        self.prog = prog
        self.logger = None
        self.do_load_config()

    def do_load_config(self):
        """
        Loads the configuration
        """
        try:
            self.load_default_config()
            self.load_config()
        except Exception as e:
            print("\nError: %s" % str(e), file=sys.stderr)
            sys.stderr.flush()
            sys.exit(1)

    def load_default_config(self):
        # init configuration
        self.cfg = Config(self.usage, prog=self.prog)

    def init(self, parser, opts, args):
        raise NotImplementedError

    def load(self):
        raise NotImplementedError

    def load_config(self):
        """
        This method is used to load the configuration from one or several input(s).
        Custom Command line, configuration file.
        You have to override this method in your class.
        """
        raise NotImplementedError

    def reload(self):
        self.do_load_config()
        if self.cfg.spew:
            debug.spew()

    def wsgi(self):
        if self.callable is None:
            self.callable = self.load()
        return self.callable

    def run(self):
        try:
            Arbiter(self).run()
        except RuntimeError as e:
            print("\nError: %s\n" % e, file=sys.stderr)
            sys.stderr.flush()
            sys.exit(1)


class Application(BaseApplication):

    # 'init' and 'load' methods are implemented by WSGIApplication.
    # pylint: disable=abstract-method

    def chdir(self):
        # chdir to the configured path before loading,
        # default is the current dir
        os.chdir(self.cfg.chdir)

        # add the path to sys.path
        if self.cfg.chdir not in sys.path:
            sys.path.insert(0, self.cfg.chdir)

    def get_config_from_filename(self, filename):

        if not os.path.exists(filename):
            raise RuntimeError("%r doesn't exist" % filename)

        ext = os.path.splitext(filename)[1]

        try:
            module_name = '__config__'
            if ext in [".py", ".pyc"]:
                spec = importlib.util.spec_from_file_location(module_name, filename)
            else:
                msg = "configuration file should have a valid Python extension.\n"
                util.warn(msg)
                loader_ = importlib.machinery.SourceFileLoader(module_name, filename)
                spec = importlib.util.spec_from_file_location(module_name, filename, loader=loader_)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
        except Exception:
            print("Failed to read config file: %s" % filename, file=sys.stderr)
            traceback.print_exc()
            sys.stderr.flush()
            sys.exit(1)

        return vars(mod)

    def get_config_from_module_name(self, module_name):
        return vars(importlib.import_module(module_name))

    def load_config_from_module_name_or_filename(self, location):
        """
        Loads the configuration file: the file is a python file, otherwise raise an RuntimeError
        Exception or stop the process if the configuration file contains a syntax error.
        """

        if location.startswith("python:"):
            module_name = location[len("python:"):]
            cfg = self.get_config_from_module_name(module_name)
        else:
            if location.startswith("file:"):
                filename = location[len("file:"):]
            else:
                filename = location
            cfg = self.get_config_from_filename(filename)

        for k, v in cfg.items():
            # Ignore unknown names
            if k not in self.cfg.settings:
                continue
            try:
                self.cfg.set(k.lower(), v)
            except Exception:
                print("Invalid value for %s: %s\n" % (k, v), file=sys.stderr)
                sys.stderr.flush()
                raise

        return cfg

    def load_config_from_file(self, filename):
        return self.load_config_from_module_name_or_filename(location=filename)

    def load_config(self):
        # parse console args
        parser = self.cfg.parser()
        args = parser.parse_args()

        # optional settings from apps
        cfg = self.init(parser, args, args.args)

        # set up import paths and follow symlinks
        self.chdir()

        # Load up the any app specific configuration
        if cfg:
            for k, v in cfg.items():
                self.cfg.set(k.lower(), v)

        env_args = parser.parse_args(self.cfg.get_cmd_args_from_env())

        if args.config:
            self.load_config_from_file(args.config)
        elif env_args.config:
            self.load_config_from_file(env_args.config)
        else:
            default_config = get_default_config_file()
            if default_config is not None:
                self.load_config_from_file(default_config)

        # Load up environment configuration
        for k, v in vars(env_args).items():
            if v is None:
                continue
            if k == "args":
                continue
            self.cfg.set(k.lower(), v)

        # Lastly, update the configuration with any command line settings.
        for k, v in vars(args).items():
            if v is None:
                continue
            if k == "args":
                continue
            self.cfg.set(k.lower(), v)

        # current directory might be changed by the config now
        # set up import paths and follow symlinks
        self.chdir()

    def run(self):
        # gaojian: 是否需要打印配置信息
        if self.cfg.print_config:
            print(self.cfg)

        # gaojian: check_config 参数用于检查配置文件的有效性。
        # gaojian: 当启用此参数时，Gunicorn 会加载并验证配置文件，但不会启动服务器。
        if self.cfg.print_config or self.cfg.check_config:
            try:
                self.load()
            except Exception:
                msg = "\nError while loading the application:\n"
                print(msg, file=sys.stderr)
                traceback.print_exc()
                sys.stderr.flush()
                sys.exit(1)
            sys.exit(0)

        # gaojian: spew参数用于调试目的
        # gaojian: spew参数是一个布尔值，用于控制是否打印出当前进程的所有线程的栈信息
        # gaojian: 如果设置了spew参数，gunicorn会记录每个Python语句的执行情况，这对于调试和性能分析非常有用，但会显著降低性能
        # gaojian: 如果设置了spew参数，将会打印出当前进程的所有线程的栈信息
        # gaojian: 通过这些信息，可以看到当前进程的所有线程的调用栈信息
        # gaojian: 这对于调试多线程程序非常有用
        # gaojian: 但是，由于打印的信息非常多，所以不建议在生产环境中使用
        if self.cfg.spew:
            debug.spew()

        if self.cfg.daemon:
            if os.environ.get('NOTIFY_SOCKET'):
                # 当使用 systemd 启动进程并配置了 Type = notify 时，
                # 不要将进程配置为守护进程（daemon = True），
                # 因为这会导致 systemd 无法正确跟踪进程的状态。
                # 推荐使用 Type = simple 作为替代方案。

                # systemd 会有这个问题吗？
                # 是的，systemd 确实会有这个问题。
                # 具体来说，当你使用 systemd 启动进程并配置了 Type=notify 时，
                # 如果进程被配置为守护进程（daemon=True），systemd 将无法正确跟踪进程的状态。
                # 这是因为守护进程通常会脱离终端，导致 systemd 无法接收到进程的状态通知。
                
                # Type=notify：这种类型表示进程会在启动后通过 sd_notify 向 systemd 发送通知，告知其状态（例如启动完成、正在运行等）。
                # systemd 依赖这些通知来跟踪进程的状态。
                # 例如，当进程启动后，可以通过 sd_notify(0, "READY=1") 向 systemd 发送一个 READY=1 的通知，
                # 以告知 systemd 进程已经启动完成。这样 systemd 就可以知道进程已经启动完成了。

                # 当进程被配置为守护进程时，systemd 无法接收到 sd_notify 发送的状态通知，导致 systemd 无法正确跟踪进程的状态。
                # 此时可以使用 Type=simple 代替 Type=notify，这样 systemd 就不会依赖进程发送的通知来跟踪进程的状态。
                # Type=simple 表示 systemd 只会启动进程并认为它已经启动完成，而不需要进程发送任何通知。
                msg = (
                    "Warning: you shouldn't specify `daemon = True` when launching by systemd with `Type = notify` "
                    "because systemd will not be able to track the status of the process. "
                    "It is recommended to use `Type = simple` instead. "
                    "See https://www.freedesktop.org/software/system"
                )
                print(msg, file=sys.stderr, flush=True)
            
            util.daemonize(self.cfg.enable_stdio_inheritance)

        # set python paths
        if self.cfg.pythonpath:
            paths = self.cfg.pythonpath.split(",")
            for path in paths:
                pythonpath = os.path.abspath(path)
                if pythonpath not in sys.path:
                    sys.path.insert(0, pythonpath)

        super().run()
