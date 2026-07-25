"""rebe-agent - the bien.mx WhatsApp news agent.

Deployment shape is fixed by `docs/wayfinder/deployment-architecture-spec.md`:
one process, one replica, two triggers (Evolution webhook + scheduled news),
one shared pacer.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
