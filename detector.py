from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import DEAD_DISPATCHER
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
from ryu.lib.packet import packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_link
from ryu.topology.api import get_switch


APP_COOKIE = 0x2100000000000021
FLOW_PRIORITY = 100
FLOW_IDLE_TIMEOUT = 30


class TopologyChangeDetector(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.mac_to_port = {}
        self.switches = []
        self.links = []

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)
            self.mac_to_port.pop(datapath.id, None)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, parser.OFPMatch(), actions, cookie=0)
        self.logger.info("Installed table-miss rule on s%s", datapath.id)

    def add_flow(
        self,
        datapath,
        priority,
        match,
        actions,
        cookie=APP_COOKIE,
        idle_timeout=0,
        hard_timeout=0,
        buffer_id=None,
    ):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        instructions = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        kwargs = {
            "datapath": datapath,
            "priority": priority,
            "match": match,
            "instructions": instructions,
            "cookie": cookie,
            "idle_timeout": idle_timeout,
            "hard_timeout": hard_timeout,
        }
        if buffer_id is not None and buffer_id != ofproto.OFP_NO_BUFFER:
            kwargs["buffer_id"] = buffer_id
        datapath.send_msg(parser.OFPFlowMod(**kwargs))

    def clear_dynamic_state(self, reason):
        for datapath in self.datapaths.values():
            parser = datapath.ofproto_parser
            ofproto = datapath.ofproto
            flow_mod = parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=parser.OFPMatch(),
                cookie=APP_COOKIE,
                cookie_mask=0xFFFFFFFFFFFFFFFF,
                table_id=ofproto.OFPTT_ALL,
            )
            datapath.send_msg(flow_mod)
            datapath.send_msg(parser.OFPBarrierRequest(datapath))
        self.mac_to_port.clear()
        self.logger.info("Cleared learned flows after %s", reason)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        if eth.ethertype not in (ether_types.ETH_TYPE_ARP, ether_types.ETH_TYPE_IP):
            return

        dpid = datapath.id
        src = eth.src
        dst = eth.dst

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst)
            self.add_flow(
                datapath,
                FLOW_PRIORITY,
                match,
                actions,
                cookie=APP_COOKIE,
                idle_timeout=FLOW_IDLE_TIMEOUT,
                buffer_id=msg.buffer_id,
            )
            self.logger.info(
                "Installed flow on s%s: %s -> %s via port %s",
                dpid,
                src,
                dst,
                out_port,
            )
        else:
            self.logger.info("Flooding packet on s%s for unknown destination %s", dpid, dst)

        data = None if msg.buffer_id != ofproto.OFP_NO_BUFFER else msg.data
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)

    def update_topology(self, trigger, details, clear_state=False):
        self.switches = sorted(switch.dp.id for switch in get_switch(self, None))

        links = []
        seen = set()
        for link in get_link(self, None):
            left = (link.src.dpid, link.src.port_no)
            right = (link.dst.dpid, link.dst.port_no)
            edge = tuple(sorted((left, right)))
            if edge in seen:
                continue
            seen.add(edge)
            links.append(edge)
        self.links = sorted(links)

        if clear_state:
            self.clear_dynamic_state(trigger)

        self.logger.info(
            "%s | switches=%s links=%s | %s",
            trigger.upper(),
            len(self.switches),
            len(self.links),
            details,
        )

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        dpid = ev.switch.dp.id
        self.update_topology("switch_enter", f"{dpid} joined", clear_state=True)

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        self.datapaths.pop(dpid, None)
        self.mac_to_port.pop(dpid, None)
        self.update_topology("switch_leave", f"{dpid} left", clear_state=True)

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        link = ev.link
        detail = f"s{link.src.dpid}:{link.src.port_no} <-> s{link.dst.dpid}:{link.dst.port_no}"
        self.update_topology("link_add", detail, clear_state=True)

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        link = ev.link
        detail = f"s{link.src.dpid}:{link.src.port_no} <-> s{link.dst.dpid}:{link.dst.port_no}"
        self.update_topology("link_delete", detail, clear_state=True)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        reason = {
            msg.datapath.ofproto.OFPPR_ADD: "ADD",
            msg.datapath.ofproto.OFPPR_DELETE: "DELETE",
            msg.datapath.ofproto.OFPPR_MODIFY: "MODIFY",
        }.get(msg.reason, f"UNKNOWN({msg.reason})")
        name = msg.desc.name.decode("utf-8", errors="ignore").strip("\x00")
        self.logger.info(
            "PORT_STATUS | switch=s%s port=%s reason=%s name=%s",
            msg.datapath.id,
            msg.desc.port_no,
            reason,
            name,
        )
