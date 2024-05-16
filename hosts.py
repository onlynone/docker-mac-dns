#!/usr/bin/env python3

from socketserver import TCPServer
import threading
import time
import queue

from collections import defaultdict
from typing import Mapping
from dnserver.load_records import Records
from dnserver.main import BaseResolver, ProxyResolver, logger, DEFAULT_PORT, DEFAULT_UPSTREAM
from dnslib.server import DNSServer as LibDNSServer
import dnslib
import dnslib.zoneresolver
import dnslib.server
import docker

from dnserver import DNSServer, Zone

class DNSServerWithAddress(DNSServer):
    def __init__(self, records: Records | None = None, address: str | None = None, port: int | str | None = ..., upstream: str | None = ...):
        super().__init__(records, port, upstream)
        self.address = address

    def start(self):
        if self.address is None:
            return super().start()

        if self.upstream:
            logger.info('starting DNS server on port %d, upstream DNS server "%s"', self.port, self.upstream)
            resolver = ProxyResolver(self.records, self.upstream)
        else:
            logger.info('starting DNS server on port %d, without upstream DNS server', self.port)
            resolver = BaseResolver(self.records)

        self.udp_server = LibDNSServer(resolver, address=self.address, port=self.port)
        self.tcp_server = LibDNSServer(resolver, address=self.address, port=self.port, tcp=True)
        self.udp_server.start_thread()
        self.tcp_server.start_thread()

# dns_server = DNSServerWithAddress(address="127.0.0.1", port=0, upstream=None)


zone_resolver = dnslib.zoneresolver.ZoneResolver("")
udp_server = dnslib.server.DNSServer(
    zone_resolver,
    port=0,
    address="127.0.0.1")

client = docker.from_env()
container_starts = queue.Queue()
network_disconnects = queue.Queue()
host_updates = queue.Queue()

def gather_events(container_starts):
    for event in client.events(decode=True):
        if event["Type"] == "container" and event["Action"] == "start":
            container_starts.put(event)
        elif event["Type"] == "network" and event["Action"] == "connect":
            # TODO: use this rather than container start
            pass
        elif event["Type"] == "network" and event["Action"] == "disconnect":
            network_disconnects.put(event)

def process_container_starts(container_starts, host_updates):
    while True:
        container_start = container_starts.get()
        container = client.containers.get(container_start["id"])
        for (network_name, network_settings) in container.attrs['NetworkSettings']['Networks'].items():
            host_updates.put(
                {
                 "action": "add",
                 "address": network_settings['IPAddress'],
                 "names": [f"{dns_name}.{network_name}" for dns_name in network_settings["DNSNames"]],
                 "definitive": True,
                }
            )

        container_starts.task_done()

def process_network_disconnects(network_disconnects, host_updates):
    while True:
        network_disconnect = network_disconnects.get()
        container = client.containers.get(network_disconnect["Actor"]["Attributes"]["container"])
        for (network_name, network_settings) in container.attrs['NetworkSettings']['Networks'].items():
            host_updates.put(
                {
                 "action": "remove",
                 "names": [f"{dns_name}.{network_name}" for dns_name in network_settings["DNSNames"]],
                 "definitive": True,
                }
            )

        network_disconnects.task_done()

def process_host_updates(host_updates, dns_server_address):
    names: Mapping[str,str] = {}
    addresses: Mapping[str,Mapping[str,True]] = defaultdict(dict)
    updated = False

    while True:
        try:
            host_update = host_updates.get(timeout=5)
        except queue.Empty:
            if updated:
                with open("/Users/steven/.local/share/docker-dns/hosts", "w") as hosts_file:
                    for (address, hostnames) in addresses.items():
                        if len(hostnames) > 0:
                            print(f"{address} {' '.join(hostnames)}", file=hosts_file)
                new_dns_server_records = []
                for (hostname, address) in names.items():
                    rr = dnslib.RR(rname=hostname, rtype=dnslib.QTYPE.A, rclass=dnslib.CLASS.IN, ttl=5, rdata=dnslib.A(address))
                    new_dns_server_records.append((rr.rname,dnslib.QTYPE[rr.rtype],rr))
                zone_resolver.zone = new_dns_server_records

                for domain in {hostname.split('.')[-1] for hostname in names}:
                    with open(f"/etc/resolver/{domain}", "w") as f:
                        print(f"nameserver {dns_server_address[0]}", file=f)
                        print(f"port {dns_server_address[1]}", file=f)

                updated = False

            continue

        if host_update["action"] == "add":
            for name in host_update["names"]:
                if host_update["definitive"] or name not in names:
                    addresses[host_update["address"]][name] = True
                    names[name] = host_update["address"]
                    updated = True
        elif host_update["action"] == "remove":
            for name in host_update["names"]:
                if (address := names.get(name)) is not None:
                    del addresses[address][name]
                    del names[name]
                    updated = True

def backfill_all_hosts(host_updates):
    time.sleep(1)
    for container in client.containers.list():
        for (network_name, network_settings) in container.attrs['NetworkSettings']['Networks'].items():
            host_updates.put(
                {
                 "action": "add",
                 "address": network_settings['IPAddress'],
                 "names": [f"{dns_name}.{network_name}" for dns_name in network_settings["DNSNames"]],
                 "definitive": False,
                }
            )

udp_server.start_thread()
dns_server_address = udp_server.server.server_address

gather_events_thread = threading.Thread(target=gather_events, args=(container_starts,), daemon=False)
process_container_starts_thread = threading.Thread(target=process_container_starts, args=(container_starts, host_updates), daemon=True)
process_network_disconnects_thread = threading.Thread(target=process_network_disconnects, args=(network_disconnects, host_updates), daemon=True)
process_host_updates_thread = threading.Thread(target=process_host_updates, args=(host_updates, dns_server_address), daemon=True)
backfill_all_hosts_thread = threading.Thread(target=backfill_all_hosts, args=(host_updates,), daemon=False)

process_host_updates_thread.start()
process_container_starts_thread.start()
process_network_disconnects_thread.start()
gather_events_thread.start()
backfill_all_hosts_thread.start()


gather_events_thread.join()
udp_server.stop()
udp_server.server.server_close()