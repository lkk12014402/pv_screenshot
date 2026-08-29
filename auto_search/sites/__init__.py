"""站点注册表：site 名 -> 插件类。"""
from .chaoslib import ChaoslibSite

SITES: dict[str, type[ChaoslibSite]] = {
    ChaoslibSite.name: ChaoslibSite,
}
