#!/usr/bin/env python3

import threading
import time
import queue

import docker

client = docker.from_env()
container_starts = queue.Queue()
host_updates = queue.Queue()

def gather_events(container_starts):
    for event in client.events(decode=True):
        if event["Type"] == "container" and event["Action"] == "start":
            container_starts.put(event)

def process_container_starts(container_starts, host_updates):
    while True:
        container_start = container_starts.get()
        container = client.containers.get(container_start["id"])
        for (network_name, network_settings) in container.attrs['NetworkSettings']['Networks'].items():
            for dns_name in network_settings["DNSNames"]:
                host_updates.put(
                    {
                     "name": f"{dns_name}.{network_name}",
                     "address": network_settings['IPAddress'],
                     "definitive": True,
                    }
                )

        container_starts.task_done()

def process_host_updates(host_updates):
    hosts = {}
    updated = False
    while True:
        try:
            host_update = host_updates.get(timeout=5)
        except queue.Empty:
            if updated:
                print("# /etc/hosts START")
                for (name, address) in sorted(hosts.items(), key=lambda x: (x[1], x[0])):
                    print(f"{address} {name}")
                print("# /etc/hosts DONE")
                updated = False
            continue
        if host_update["definitive"] or host_update["name"] not in hosts:
            hosts[host_update["name"]] = host_update["address"]
            updated = True

def backfill_all_hosts(host_updates):
    time.sleep(20)
    for container in client.containers.list():
        for (network_name, network_settings) in container.attrs['NetworkSettings']['Networks'].items():
            print(network_settings["DNSNames"])
            for dns_name in network_settings["DNSNames"]:
                host_updates.put(
                    {
                     "name": f"{dns_name}.{network_name}",
                     "address": network_settings['IPAddress'],
                     "definitive": False,
                    }
                )

gather_events_thread = threading.Thread(target=gather_events, args=(container_starts,), daemon=False)
process_container_starts_thread = threading.Thread(target=process_container_starts, args=(container_starts, host_updates), daemon=True)
process_host_updates_thread = threading.Thread(target=process_host_updates, args=(host_updates,), daemon=True)
backfill_all_hosts_thread = threading.Thread(target=backfill_all_hosts, args=(host_updates,), daemon=False)

process_host_updates_thread.start()
process_container_starts_thread.start()
gather_events_thread.start()
backfill_all_hosts_thread.start()
