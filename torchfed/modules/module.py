import os

import urllib3

import visdom

from torchfed.routers.router_msg import RouterMsg, RouterMsgResponse
from typing import TypeVar, Generic
from torchfed.logging import get_logger
from torchfed.types.meta import PostInitCaller

from prettytable import PrettyTable

T = TypeVar('T')


class Module(metaclass=PostInitCaller):
    def __init__(self, name, router, visualizer=False, debug=False):
        self.name = name
        self.debug = debug
        self.router = router
        self.logger = get_logger(router.ident, self.get_root_name())

        self.visualizer = visualizer
        if self.visualizer:
            if self.is_root():
                self.logger.info(
                    f"[{self.name}] Visualizer enabled. Run `visdom -env_path=./runs` to start.")
            self.writer = self.get_visualizer()

        self.routing_table = {}
        router.register(self)

        self.execute_gen = self.execute()

    def __post__init__(self):
        if self.is_root():
            hps = self.log_hp()
            hps["name"] = self.name
            hps["visualizer"] = self.visualizer
            hps["debug"] = self.debug
            hp_table = PrettyTable()
            hp_table.field_names = hps.keys()
            hp_table.add_row(hps.values())
            for row in hp_table.get_string().split("\n"):
                self.logger.info(row)

    def __call__(self, *args, **kwargs):
        _continue = next(self.execute_gen)
        if not _continue:
            self.execute_gen = self.execute()
        return _continue

    def log_hp(self):
        return {}

    def execute(self):
        yield False

    def register_submodule(self, module: Generic[T], name, router, *args) -> T:
        submodule_name = f"{self.name}/{name}"
        if submodule_name in self.routing_table:
            self.logger.error("Cannot register modules with the same name")
            raise Exception("Cannot register modules with the same name")
        module_obj = module(
            submodule_name,
            router,
            *args,
            visualizer=self.visualizer,
            debug=self.debug)
        self.routing_table[name] = module_obj
        return module_obj

    def send(self, to, path, args):
        if callable(path):
            path = f"{'/'.join(path.__self__.name.split('/')[1:])}/{path.__name__}"
        router_msg = RouterMsg(from_=self.name, to=to, path=path, args=args)
        return self.router.broadcast(router_msg)

    def receive(self, router_msg: RouterMsg) -> RouterMsgResponse:
        if self.debug:
            self.logger.debug(
                f"Module {self.name} receiving data {router_msg}")
        if self.debug:
            self.logger.debug(
                f"Module {self.name} is calling path {router_msg.path} with args {router_msg.args}")

        ret = RouterMsgResponse(
            from_=self.name,
            to=router_msg.from_,
            data=self.manual_call(
                router_msg.path,
                router_msg.args))

        if ret.data is None:
            self.logger.warning(
                f"Module {self.name} does not have path {router_msg.path}")
        if self.debug:
            self.logger.debug(f"Module {self.name} responses with data {ret}")
        return ret

    def manual_call(self, path, args, check_exposed=True):
        if callable(path):
            path = f"{'/'.join(path.__self__.name.split('/')[1:])}/{path.__name__}"

        paths = path.split("/")
        target = paths.pop(0)

        if target in self.routing_table:
            return self.routing_table[target].manual_call(
                "/".join(paths), args, check_exposed=check_exposed)
        elif hasattr(self, target):
            entrance = getattr(self, target)
            if callable(entrance) and (
                not check_exposed or (
                    hasattr(
                        entrance,
                        "exposed") and entrance.exposed)):
                return entrance(*args)
        return None

    def get_visualizer(self):
        if not os.path.exists("runs"):
            os.mkdir("runs")

        visualizer_log_path = f"runs/{self.router.ident}"
        if not os.path.exists(visualizer_log_path):
            os.mkdir(visualizer_log_path)

        try:
            v = visdom.Visdom(env=self.router.ident, log_to_filename=f"{visualizer_log_path}/{self.get_root_name()}.vis", raise_exceptions=True)
        except (ConnectionRefusedError, ConnectionError, urllib3.exceptions.NewConnectionError, urllib3.exceptions.MaxRetryError):
            if self.is_root():
                self.logger.warning("Visualizer server has to be started ahead of time")
                self.logger.warning(
                    f"Using offline mode, visualizer logs to runs/{self.router.ident}/{self.get_root_name()}.vis")
            v = visdom.Visdom(env=self.router.ident,
                              log_to_filename=f"{visualizer_log_path}/{self.get_root_name()}.vis", offline=True)
        return v

    def is_root(self):
        return "/" not in self.name

    def get_root_name(self):
        return self.name.split("/")[0]

    def get_path(self):
        return "/".join(self.name.split("/")[1:])

    def __del__(self):
        if self.visualizer:
            self.writer.close()
