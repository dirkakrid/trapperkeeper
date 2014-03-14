from datetime import timedelta
import logging
from oid_translate import ObjectId

from pyasn1.codec.ber import decoder
from pysnmp.proto import api
import socket

from trapperkeeper.constants import SNMP_VERSIONS
from trapperkeeper.models import Notification
from trapperkeeper.utils import parse_time_string, send_trap_email, hostname_or_ip


class TrapperCallback(object):
    def __init__(self, conn, template_env, config):
        self.conn = conn
        self.template_env = template_env
        self.config = config
        self.hostname = socket.gethostname()

    def __call__(self, *args, **kwargs):
        try:
            self._call(*args, **kwargs)
        # Prevent the application from crashing when callback raises
        # an exception.
        except Exception as err:
            logging.exception("Callback Failed: %s", err)

    def _send_mail(self, handler, trap):
        mail = handler["mail"]
        if not mail:
            return

        recipients = handler["mail"].get("recipients")
        if not recipients:
            return

        subject = handler["mail"]["subject"] % {
            "trap_oid": trap.oid,
            "trap_name": ObjectId(trap.oid).name,
            "ipaddress": trap.host,
            "hostname": hostname_or_ip(trap.host),
        }
        ctxt = dict(trap=trap, dest_host=self.hostname)
        send_trap_email(recipients, "trapperkeeper@dropbox.com",
                        subject, self.template_env, ctxt)

    def _call(self, transport_dispatcher, transport_domain, transport_address, whole_msg):
        if not whole_msg:
            return

        msg_version = int(api.decodeMessageVersion(whole_msg))

        if msg_version in api.protoModules:
            proto_module = api.protoModules[msg_version]
        else:
            logging.error("Unsupported SNMP version %s", msg_version)
            return

        req_msg, whole_msg = decoder.decode(whole_msg, asn1Spec=proto_module.Message(),)
        host = transport_address[0]
        req_pdu = proto_module.apiMessage.getPDU(req_msg)
        # community = proto_module.apiMessage.getCommunity(req_msg)
        version = SNMP_VERSIONS[msg_version]

        if not req_pdu.isSameTypeWith(proto_module.TrapPDU()):
            logging.warning("Received non-trap notification from %s", host)
            return

        if msg_version not in (api.protoVersion1, api.protoVersion2c):
            logging.warning("Received trap not in v1 or v2c")
            return

        trap = Notification.from_pdu(host, proto_module, version, req_pdu)
        handler = self.config.handlers[trap.oid]

        if handler.get("expiration", None):
            expires = parse_time_string(handler["expiration"])
            expires = timedelta(**expires)
            trap.expires = trap.sent + expires

        if trap is None:
            logging.warning("Invalid trap from %s: %s", host, req_pdu)
            return

        objid = ObjectId(trap.oid)
        if handler.get("blackhole", False):
            logging.debug("Blackholed %s from %s", objid.name, host)
            return

        logging.info("Trap Received (%s) from %s", objid.name, host)

        self.conn.add(trap)
        self.conn.commit()
        self._send_mail(handler, trap)