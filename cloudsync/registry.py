import sys

__all__ = ["create_provider"]

providers = {}


def register_provider(prov):
    providers[prov.name] = prov


def discover_providers():
    for m in sys.modules:
        mod = sys.modules[m]
        if hasattr(mod, "__cloudsync__"):
            if mod.__cloudsync__.name not in providers:
                register_provider(mod.__cloudsync__)


def provider_by_name(name):
    if name not in providers:
        discover_providers()

    if name not in providers:
        raise RuntimeError("%s not a registered provider, maybe you forgot to import cloudsync_%s" % (name, name))

    return providers[name]


def create_provider(name, *args, **kws):
    return provider_by_name(name)(*args, *kws)


def known_providers():
    discover_providers()
    return list(providers.keys())
