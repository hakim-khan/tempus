import random
import time
import sys
import json
from expiringdict import ExpiringDict
from utils.helpers import config, logger, hasher, standard_encode
from urllib.parse import urlparse
from utils.pki import sign
import requests


class Messenger(object):
    def __init__(self):
        self.peers = {}

        # cache to avoid processing duplicate json forwards
        self.duplicate_cache = ExpiringDict(
            max_len=config['expiring_dict_max_len'],
            max_age_seconds=config['expiring_dict_max_age'])

    def check_duplicate(self, values):
        # Check if dict values has been received in the past x seconds
        # already..
        if self.duplicate_cache.get(hasher(values)):
            return True
        else:
            self.duplicate_cache[hasher(values)] = True
            return False

    def register_peer(self, url, peer_addr):
        """
        Add a new peer to the list of peers

        :param url: <str> Address of peer. Eg. 'http://192.168.0.5:5000'
        :param peer_addr: <str> Mining addr of peer
        :return: <bool> Whether it was already in list or not
        """
        netloc = urlparse(url).netloc

        netloc = "http://" + netloc

        # Avoid adding self
        if peer_addr == self.addr:
            return False

        # Avoid adding already existing netloc
        if netloc in self.peers:
            return False

        self.peers[netloc] = peer_addr
        return True

    def forward(self, data_dict, route, origin=None, redistribute=0):
        """
        Forward any json content to another peer

        :param data_dict: dictionary which becomes json content
        :param route: which route it's addressed at
            (for ex, forwarding a txn, a peer, etc)
        :param origin: origin of this forward
        :param redistribute: Amount of hops (redistributions through peers)
            this json message has passed through
        :return: void
        """
        # TODO: Right now max hops is set to 1.... meaning no redistribution.
        # Good cause we have full netw connectivity
        # TODO: However for nonfully connected nodes, > 1 hops needed to fully
        # reach all corners and nodes of network

        # Dont forward to peers if exceeding certain amount of hops
        if redistribute < config['max_hops']:
            # TODO: What happens if malicious actor fakes the ?addr= ?? or the
            # amount of hops?
            for peer in self.peers:
                try:  # Add self.addr in query to identify self to peers
                    # If origin addr is not target peer addr
                    if origin != self.peers[peer]:
                        requests.post(
                            peer + '/forward/' + route + '?addr=' + origin +
                            "&redistribute=" + str(redistribute + 1),
                            json=data_dict, timeout=config['timeout'])
                except Exception as e:
                    logger.debug(str(sys.exc_info()))
                    pass

    def unregister_peer(self, url):
        netloc = urlparse(url).netloc
        del self.peers[netloc]


def send_mutual_add_requests(peers, get_further_peers=False):
    # Preparing a set of further peers to possibly add later on
    peers_of_peers = set()

    # Mutual add peers
    for peer in peers:
        if peer not in messenger.peers:
            content = {"port": port, 'pubkey': clockchain.pubkey}
            signature = sign(standard_encode(content), clockchain.privkey)
            content['signature'] = signature
            try:
                response = requests.post(
                    peer + '/mutual_add',
                    json=content,
                    timeout=config['timeout'])
                peer_addr = response.text
                status_code = response.status_code
                logger.info("contacted " +
                            str(peer_addr) + ", received " +
                            str(status_code))
            except Exception as e:
                logger.debug("no response from peer: " + str(sys.exc_info()))
                continue
            if status_code == 201:
                logger.info("Adding peer " + str(peer))
                messenger.register_peer(peer, peer_addr)

                # Get all peers of current discovered peers and add to set
                # (set is to avoid duplicates)
                # Essentially going one degree further out in network. From
                # current peers to their peers
                if get_further_peers:
                    next_peers = json.loads(
                        requests.get(peer + '/info/peers').text)
                    for next_peer in next_peers['peers']:
                        peers_of_peers.add(next_peer)

    return list(peers_of_peers)


def join_network_worker():
    # Sleeping random amount to not have seed-clash (cannot do circular adding
    # of peers at the exact same time as seeds)
    sleeptime = random.randrange(3000) / 1000.0
    logger.debug("Sleeping for " + str(sleeptime) + "s before joining network")
    time.sleep(sleeptime)

    # First add seeds, and get the seeds peers
    peers_of_seeds = send_mutual_add_requests(
        config['seeds'], get_further_peers=True)

    # Then add the peers of seeds
    # TODO: Have seeds only return max 8 randomly chosen peers?
    send_mutual_add_requests(peers_of_seeds)

    logger.debug("Peers: " + str(messenger.peers))

    # TODO: Synchronizing latest chain with peers (choosing what the majority?)

    logger.debug("Finished joining network")