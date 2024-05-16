#!/usr/bin/env python3

import threading
import time
import queue

from collections import defaultdict
import docker

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

def process_host_updates(host_updates):
    names = {}
    addresses = defaultdict(dict)
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

gather_events_thread = threading.Thread(target=gather_events, args=(container_starts,), daemon=False)
process_container_starts_thread = threading.Thread(target=process_container_starts, args=(container_starts, host_updates), daemon=True)
process_network_disconnects_thread = threading.Thread(target=process_network_disconnects, args=(network_disconnects, host_updates), daemon=True)
process_host_updates_thread = threading.Thread(target=process_host_updates, args=(host_updates,), daemon=True)
backfill_all_hosts_thread = threading.Thread(target=backfill_all_hosts, args=(host_updates,), daemon=False)

process_host_updates_thread.start()
process_container_starts_thread.start()
process_network_disconnects_thread.start()
gather_events_thread.start()
backfill_all_hosts_thread.start()
