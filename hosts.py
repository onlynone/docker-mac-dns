#!/usr/bin/env python3

import os
from socketserver import TCPServer
import threading
import time
import queue

from collections import defaultdict
from typing import Mapping
import dnslib
import dnslib.zoneresolver
import dnslib.server
import docker

resolver_dir = "/etc/resolver"
os.makedirs(resolver_dir, exist_ok=True)

user_share_dir = os.path.expanduser("~/.local/share/docker-dns/")
os.makedirs(user_share_dir, exist_ok=True)
user_share_hosts_filename = os.path.join(user_share_dir, "hosts")

zone_resolver = dnslib.zoneresolver.ZoneResolver("")
udp_server = dnslib.server.DNSServer(
    zone_resolver,
    port=0,
    address="127.0.0.1")

client = docker.from_env()
container_starts = queue.Queue()
network_disconnects = queue.Queue()
host_updates = queue.Queue()
file_paths_created = queue.Queue()

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

def process_host_updates(host_updates, dns_server_address, file_paths_created):
    names: Mapping[str,str] = {}
    addresses: Mapping[str,Mapping[str,True]] = defaultdict(dict)
    updated = False

    while True:
        try:
            host_update = host_updates.get(timeout=5)
        except queue.Empty:
            if updated:
                with open(user_share_hosts_filename, "w") as hosts_file:
                    file_paths_created.put(user_share_hosts_filename)
                    for (address, hostnames) in addresses.items():
                        if len(hostnames) > 0:
                            print(f"{address} {' '.join(hostnames)}", file=hosts_file)
                zone: list[tuple[str, str, dnslib.RR]] = []
                for (hostname, address) in names.items():
                    rr = dnslib.RR(rname=hostname, rtype=dnslib.QTYPE.A, rclass=dnslib.CLASS.IN, ttl=5, rdata=dnslib.A(address))
                    zone.append((rr.rname,dnslib.QTYPE[rr.rtype],rr))
                zone_resolver.zone = zone

                for domain in {hostname.split('.')[-1] for hostname in names}:
                    domain_filename = os.path.join(resolver_dir, domain)
                    if  os.path.dirname(domain_filename) != resolver_dir:
                        raise RuntimeError(f"domain: {domain} would write to a file: {domain_filename} that is not under the resolver dir: {resolver_dir}")
                    with open(domain_filename, "w") as f:
                        file_paths_created.put(f.name)
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
process_host_updates_thread = threading.Thread(target=process_host_updates, args=(host_updates, dns_server_address, file_paths_created), daemon=True)
backfill_all_hosts_thread = threading.Thread(target=backfill_all_hosts, args=(host_updates,), daemon=False)

process_host_updates_thread.start()
process_container_starts_thread.start()
process_network_disconnects_thread.start()
gather_events_thread.start()
backfill_all_hosts_thread.start()

try:
    gather_events_thread.join()
finally:
    udp_server.stop()
    udp_server.server.server_close()
    file_paths = set()
    while True:
        try:
            file_paths.add(file_paths_created.get(timeout=0.1))
        except queue.Empty:
            break

    for file_path in file_paths:
        os.unlink(file_path)
