"""站点注册表：site 名 -> 插件类。"""
from .chaoslib import ChaoslibSite
from .wanfang import WanfangSite

SITES: dict[str, type[ChaoslibSite]] = {
    ChaoslibSite.name: ChaoslibSite,
    WanfangSite.name: WanfangSite,
}
