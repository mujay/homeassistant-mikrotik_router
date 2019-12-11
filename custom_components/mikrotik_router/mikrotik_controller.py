"""Mikrotik Controller for Mikrotik Router."""

from datetime import timedelta
import logging
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    CONF_TRACK_ARP,
    DEFAULT_TRACK_ARP,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
)

from .mikrotikapi import MikrotikAPI
from .helper import from_entry, from_entry_bool, from_list

_LOGGER = logging.getLogger(__name__)


# ---------------------------
#   MikrotikControllerData
# ---------------------------
class MikrotikControllerData():
    """MikrotikController Class"""
    def __init__(self, hass, config_entry, name, host, port, username, password, use_ssl):
        """Initialize MikrotikController."""
        self.name = name
        self.hass = hass
        self.config_entry = config_entry

        self.data = {'routerboard': {},
                     'resource': {},
                     'interface': {},
                     'arp': {},
                     'nat': {},
                     'fw-update': {},
                     'script': {}
                     }

        self.listeners = []

        self.api = MikrotikAPI(host, username, password, port, use_ssl)

        async_track_time_interval(self.hass, self.force_update, self.option_scan_interval)
        async_track_time_interval(self.hass, self.force_fwupdate_check, timedelta(hours=1))

        return

    # ---------------------------
    #   force_update
    # ---------------------------
    async def force_update(self, _now=None):
        """Trigger update by timer"""
        await self.async_update()
        return

    # ---------------------------
    #   force_fwupdate_check
    # ---------------------------
    async def force_fwupdate_check(self, _now=None):
        """Trigger hourly update by timer"""
        await self.async_fwupdate_check()
        return

    # ---------------------------
    #   option_track_arp
    # ---------------------------
    @property
    def option_track_arp(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(CONF_TRACK_ARP, DEFAULT_TRACK_ARP)

    # ---------------------------
    #   option_scan_interval
    # ---------------------------
    @property
    def option_scan_interval(self):
        """Config entry option scan interval."""
        scan_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return timedelta(seconds=scan_interval)

    # ---------------------------
    #   signal_update
    # ---------------------------
    @property
    def signal_update(self):
        """Event to signal new data."""
        return "{}-update-{}".format(DOMAIN, self.name)

    # ---------------------------
    #   connected
    # ---------------------------
    def connected(self):
        """Return connected state"""
        return self.api.connected()

    # ---------------------------
    #   hwinfo_update
    # ---------------------------
    async def hwinfo_update(self):
        """Update Mikrotik hardware info"""
        self.get_system_routerboard()
        self.get_system_resource()
        return

    # ---------------------------
    #   async_fwupdate_check
    # ---------------------------
    async def async_fwupdate_check(self):
        """Update Mikrotik data"""

        self.get_firmware_update()

        async_dispatcher_send(self.hass, self.signal_update)
        return

    # ---------------------------
    #   async_update
    # ---------------------------
    async def async_update(self):
        """Update Mikrotik data"""

        if 'available' not in self.data['fw-update']:
            await self.async_fwupdate_check()

        await self.get_interface()
        await self.get_interface_client()
        self.get_nat()
        self.get_system_resource()
        self.get_script()

        async_dispatcher_send(self.hass, self.signal_update)
        return

    # ---------------------------
    #   async_reset
    # ---------------------------
    async def async_reset(self):
        """Reset dispatchers"""
        for unsub_dispatcher in self.listeners:
            unsub_dispatcher()

        self.listeners = []
        return True

    # ---------------------------
    #   set_value
    # ---------------------------
    def set_value(self, path, param, value, mod_param, mod_value):
        """Change value using Mikrotik API"""
        return self.api.update(path, param, value, mod_param, mod_value)

    # ---------------------------
    #   run_script
    # ---------------------------
    def run_script(self, name):
        """Run script using Mikrotik API"""
        return self.api.run_script(name)

    # ---------------------------
    #   get_interface
    # ---------------------------
    async def get_interface(self):
        """Get all interfaces data from Mikrotik"""
        self.data['interface'] = await from_list(
            data=self.data['interface'],
            source=await self.hass.async_add_executor_job(self.api.path, "/interface"),
            key='default-name',
            vals=[
                {'name': 'default-name'},
                {'name': 'name', 'default_val': 'default-name'},
                {'name': 'type', 'default': 'unknown'},
                {'name': 'running', 'type': 'bool'},
                {'name': 'enabled', 'source': 'disabled', 'type': 'bool', 'reverse': True},
                {'name': 'port-mac-address', 'source': 'mac-address'},
                {'name': 'comment'},
                {'name': 'last-link-down-time'},
                {'name': 'last-link-up-time'},
                {'name': 'link-downs'},
                {'name': 'tx-queue-drop'},
                {'name': 'actual-mtu'}
            ],
            ensure_vals=[
                {'name': 'client-ip-address'},
                {'name': 'client-mac-address'},
                {'name': 'rx-bits-per-second', 'default': 0},
                {'name': 'tx-bits-per-second', 'default': 0}
            ]
        )

        await self.get_interface_traffic(interface_list)
        return

    # ---------------------------
    #   get_interface_traffic
    # ---------------------------
    async def get_interface_traffic(self, interface_list):
        """Get traffic for all interfaces from Mikrotik"""
        interface_list = ""
        for uid in self.data['interface']:
            if interface_list:
                interface_list += ","

            interface_list += self.data['interface'][uid]['name']

        self.data['interface'] = await from_list(
            data=self.data['interface'],
            source=await self.hass.async_add_executor_job(self.api.get_traffic, interface_list),
            key_search='name',
            vals=[
                {'name': 'rx-bits-per-second', 'default': 0},
                {'name': 'tx-bits-per-second', 'default': 0},
            ]
        )
        return

    # ---------------------------
    #   get_interface_client
    # ---------------------------
    async def get_interface_client(self):
        """Get ARP data from Mikrotik"""
        self.data['arp'] = {}

        # Remove data if disabled
        if not self.option_track_arp:
            for uid in self.data['interface']:
                self.data['interface'][uid]['client-ip-address'] = "disabled"
                self.data['interface'][uid]['client-mac-address'] = "disabled"
            return False

        mac2ip = {}
        bridge_used = False
        mac2ip, bridge_used = await self.update_arp(mac2ip, bridge_used)

        if bridge_used:
            await self.update_bridge_hosts(mac2ip)

        # Map ARP to ifaces
        for uid in self.data['interface']:
            if uid not in self.data['arp']:
                continue

            self.data['interface'][uid]['client-ip-address'] = from_entry(self.data['arp'][uid], 'address')
            self.data['interface'][uid]['client-mac-address'] = from_entry(self.data['arp'][uid], 'mac-address')

        return True

    # ---------------------------
    #   update_arp
    # ---------------------------
    async def update_arp(self, mac2ip, bridge_used):
        """Get list of hosts in ARP for interface client data from Mikrotik"""
        data = await self.hass.async_add_executor_job(self.api.path, "/ip/arp")
        if not data:
            return mac2ip, bridge_used

        for entry in data:
            # Ignore invalid entries
            if entry['invalid']:
                continue

            # Do not add ARP detected on bridge
            if entry['interface'] == "bridge":
                bridge_used = True
                # Build address table on bridge
                if 'mac-address' in entry and 'address' in entry:
                    mac2ip[entry['mac-address']] = entry['address']

                continue

            # Get iface default-name from custom name
            uid = await self.get_iface_from_entry(entry)
            if not uid:
                continue

            _LOGGER.debug("Processing entry {}, entry {}".format("/interface/bridge/host", entry))
            # Create uid arp dict
            if uid not in self.data['arp']:
                self.data['arp'][uid] = {}

            # Add data
            self.data['arp'][uid]['interface'] = uid
            self.data['arp'][uid]['mac-address'] = from_entry(entry, 'mac-address') if 'mac-address' not in self.data['arp'][uid] else "multiple"
            self.data['arp'][uid]['address'] = from_entry(entry, 'address') if 'address' not in self.data['arp'][uid] else "multiple"

        return mac2ip, bridge_used

    # ---------------------------
    #   update_bridge_hosts
    # ---------------------------
    async def update_bridge_hosts(self, mac2ip):
        """Get list of hosts in bridge for interface client data from Mikrotik"""
        data = await self.hass.async_add_executor_job(self.api.path, "/interface/bridge/host")
        if not data:
            return

        for entry in data:
            # Ignore port MAC
            if entry['local']:
                continue

            # Get iface default-name from custom name
            uid = await self.get_iface_from_entry(entry)
            if not uid:
                continue

            _LOGGER.debug("Processing entry {}, entry {}".format("/interface/bridge/host", entry))
            # Create uid arp dict
            if uid not in self.data['arp']:
                self.data['arp'][uid] = {}

            # Add data
            self.data['arp'][uid]['interface'] = uid
            if 'mac-address' in self.data['arp'][uid]:
                self.data['arp'][uid]['mac-address'] = "multiple"
                self.data['arp'][uid]['address'] = "multiple"
            else:
                self.data['arp'][uid]['mac-address'] = from_entry(entry, 'mac-address')
                self.data['arp'][uid]['address'] = mac2ip[self.data['arp'][uid]['mac-address']] if self.data['arp'][uid]['mac-address'] in mac2ip else ""

        return

    # ---------------------------
    #   get_iface_from_entry
    # ---------------------------
    async def get_iface_from_entry(self, entry):
        """Get interface default-name using name from interface dict"""
        uid = None
        for ifacename in self.data['interface']:
            if self.data['interface'][ifacename]['name'] == entry['interface']:
                uid = self.data['interface'][ifacename]['default-name']
                break

        return uid

    # ---------------------------
    #   get_nat
    # ---------------------------
    def get_nat(self):
        """Get NAT data from Mikrotik"""
        data = self.api.path("/ip/firewall/nat")
        if not data:
            return

        for entry in data:
            if entry['action'] != 'dst-nat':
                continue

            uid = entry['.id']
            if uid not in self.data['nat']:
                self.data['nat'][uid] = {}

            self.data['nat'][uid]['name'] = "{}:{}".format(entry['protocol'], entry['dst-port'])
            self.data['nat'][uid]['.id'] = from_entry(entry, '.id')
            self.data['nat'][uid]['protocol'] = from_entry(entry, 'protocol')
            self.data['nat'][uid]['dst-port'] = from_entry(entry, 'dst-port')
            self.data['nat'][uid]['in-interface'] = from_entry(entry, 'in-interface', 'any')
            self.data['nat'][uid]['to-addresses'] = from_entry(entry, 'to-addresses')
            self.data['nat'][uid]['to-ports'] = from_entry(entry, 'to-ports')
            self.data['nat'][uid]['comment'] = from_entry(entry, 'comment')
            self.data['nat'][uid]['enabled'] = from_entry_bool(entry, 'disabled', default=True, reverse=True)

        return

    # ---------------------------
    #   get_system_routerboard
    # ---------------------------
    def get_system_routerboard(self):
        """Get routerboard data from Mikrotik"""
        data = self.api.path("/system/routerboard")
        if not data:
            return

        for entry in data:
            self.data['routerboard']['routerboard'] = from_entry_bool(entry, 'routerboard')
            self.data['routerboard']['model'] = from_entry(entry, 'model', 'unknown')
            self.data['routerboard']['serial-number'] = from_entry(entry, 'serial-number', 'unknown')
            self.data['routerboard']['firmware'] = from_entry(entry, 'current-firmware', 'unknown')

        return

    # ---------------------------
    #   get_system_resource
    # ---------------------------
    def get_system_resource(self):
        """Get system resources data from Mikrotik"""
        data = self.api.path("/system/resource")
        if not data:
            return

        for entry in data:
            self.data['resource']['platform'] = from_entry(entry, 'platform', 'unknown')
            self.data['resource']['board-name'] = from_entry(entry, 'board-name', 'unknown')
            self.data['resource']['version'] = from_entry(entry, 'version', 'unknown')
            self.data['resource']['uptime'] = from_entry(entry, 'uptime', 'unknown')
            self.data['resource']['cpu-load'] = from_entry(entry, 'cpu-load', 'unknown')
            if 'free-memory' in entry and 'total-memory' in entry:
                self.data['resource']['memory-usage'] = round(((entry['total-memory'] - entry['free-memory']) / entry['total-memory']) * 100)
            else:
                self.data['resource']['memory-usage'] = "unknown"

            if 'free-hdd-space' in entry and 'total-hdd-space' in entry:
                self.data['resource']['hdd-usage'] = round(((entry['total-hdd-space'] - entry['free-hdd-space']) / entry['total-hdd-space']) * 100)
            else:
                self.data['resource']['hdd-usage'] = "unknown"

        return

    # ---------------------------
    #   get_system_routerboard
    # ---------------------------
    def get_firmware_update(self):
        """Check for firmware update on Mikrotik"""
        data = self.api.path("/system/package/update")
        if not data:
            return

        for entry in data:
            if 'status' in entry:
                self.data['fw-update']['available'] = True if entry['status'] == "New version is available" else False
            elif 'available' not in self.data['fw-update']:
                self.data['fw-update']['available'] = False
            self.data['fw-update']['channel'] = from_entry(entry, 'channel', 'unknown')
            self.data['fw-update']['installed-version'] = from_entry(entry, 'installed-version', 'unknown')
            self.data['fw-update']['latest-version'] = from_entry(entry, 'latest-version', 'unknown')

        return

    # ---------------------------
    #   get_script
    # ---------------------------
    def get_script(self):
        """Get list of all scripts from Mikrotik"""
        data = self.api.path("/system/script")
        if not data:
            return

        for entry in data:
            if 'name' not in entry:
                continue

            if not entry['name']:
                _LOGGER.error("Mikrotik %s found a script without a name. It will not be available in UI.")
                continue

            uid = entry['name']
            if uid not in self.data['script']:
                self.data['script'][uid] = {}

            self.data['script'][uid]['name'] = from_entry(entry, 'name')
            self.data['script'][uid]['last-started'] = from_entry(entry, 'last-started', 'unknown')
            self.data['script'][uid]['run-count'] = from_entry(entry, 'run-count', 'unknown')

        return
