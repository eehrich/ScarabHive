"""mail plugin entrypoint."""
from .server import MailServer

PLUGIN_FACTORY = MailServer
