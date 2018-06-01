import json
import time
import random
import requests

from utils.pki import sign
from threading import Timer, Thread
from urllib.parse import urlparse

from utils.common import logger, config, credentials
from utils.helpers import standard_encode, handle_exception


class Networker(object):
    def __init__(self):

        self.peers = {}
        self.port = 0
        self.ready = False
        self.stage = "ping"  # Stages are ping->tick->vote->select
        self.join_network_thread = Thread(target=self.join_network_worker)
        # Timer for activation thread (uses resettable timer to find out port)
        self.t = Timer(config['port_timer_timeout'], self.activate)

    def activate(self):
        self.join_network_thread.start()

    def set_port(self, port):
        # This timer start and resetting necessary to get the correct port..
        # I.e. if port is blocked, this method gets called again until timer
        # is allowed to finally elapse and start the join network thread
        self.t.cancel()
        self.t = Timer(config['port_timer_timeout'], self.activate)
        self.port = port
        self.t.start()

    def register_peer(self, url, peer_addr):
        """
        Add a new peer to the list of peers

        :param url: <str> Address of peer. Eg. 'http://192.168.0.5:5000'
        :param peer_addr: <str> Mining addr of peer
        :return: <bool> Whether it was already in list or not
        """
        netloc = self.get_full_location(url)

        # Avoid adding self
        if peer_addr == credentials.addr:
            logger.debug("Cannot add self")
            return False

        # Avoid adding already existing netloc
        if netloc in self.peers:
            logger.debug("Already have this peer")
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
        # If max hops = 1, means no redistribution of data received from peers
        # Works for fully connected network (for testing purposes)
        # If max hops > 1, we redistribute data further in network
        # This is necessary when network not fully connected (as in real life)

        # Sender set forwarding flag to do-not-forward
        if redistribute == -1:
            return
        # Dont forward to peers if exceeding certain amount of hops
        if redistribute < config['max_hops']:
            redistribute = redistribute + 1
            # TODO: What happens if malicious actor fakes the ?addr= ?? or the
            # amount of hops?
            # list() used to avoid dict size change exception
            for peer in list(self.peers):
                # Check key exists + check we do not send msg back to originator
                if peer in self.peers and origin != self.peers[peer]:
                    retries = 0
                    success = False
                    while success is False:
                        try:  # Add self.addr in query to identify self to peers
                            # If origin addr is not target peer addr
                            requests.post(
                                peer + '/forward/' + route + '?addr=' + origin +
                                "&redistribute=" + str(redistribute),
                                json=data_dict, timeout=config['timeout'])
                            success = True
                        except requests.exceptions.ReadTimeout:
                            retries = retries + 1
                            time.sleep(0.5)
                            if retries > 3:
                                logger.debug(
                                    "Couldn't forward to: " + peer + ", removing")
                                self.unregister_peer(peer)
                                return
                            pass
                        except requests.exceptions.ConnectionError:
                            retries = retries + 1
                            time.sleep(0.5)
                            logger.debug("Couldn't forward to: " + peer + ", removing")
                            if retries > 3:
                                logger.debug(
                                    "Couldn't forward to: " + peer + ", removing")
                                self.unregister_peer(peer)
                                return
                            pass
                        except Exception as e:
                            retries = retries + 1
                            time.sleep(0.5)
                            if retries > 3:
                                handle_exception(e)
                                self.unregister_peer(peer)
                                return
                            pass

    def unregister_peer(self, url):
        netloc = self.get_full_location(url)
        if netloc in self.peers:
            del self.peers[netloc]

    @staticmethod
    def get_full_location(url):
        return "http://" + urlparse(url).netloc

    def get_sample_of_peers_from(self, peers, sample_size=config['max_peers']):
        peers_of_peers = set()
        # Get peers of provided peers list and add to set (set=>no duplicates)
        for peer in list(peers):
            try:
                next_peers = \
                    json.loads(requests.get(
                        peer + '/info/peers', timeout=config['timeout']).text)
                for next_peer in next_peers['peers']:
                    peers_of_peers.add(next_peer)
            except requests.exceptions.ReadTimeout:
                logger.debug("Couldn't connect to " + peer)
                pass
            except requests.exceptions.ConnectionError:
                logger.debug("Couldn't connect to " + peer)
                pass
            except Exception as e:
                handle_exception(e)
                pass

        if sample_size > len(list(peers_of_peers)):
            sample_size = len(list(peers_of_peers))

        return random.sample(list(peers_of_peers), sample_size)

    def send_mutual_add_requests(self, peerslist):
        successful_adds = 0
        # Mutual add peers
        for peer in peerslist:
            if peer not in self.peers and len(self.peers) <= config['max_peers']:
                content = {"port": self.port, 'pubkey': credentials.pubkey}
                signature = sign(standard_encode(content),
                                 credentials.privkey)
                content['signature'] = signature
                status_code = None
                try:
                    response = requests.post(
                        peer + '/mutual_add',
                        json=content,
                        timeout=config['timeout'])
                    status_code = response.status_code
                # TODO: These except clauses very repeated.. refactor into fct?
                except requests.exceptions.ReadTimeout:
                    logger.debug("Couldn't connect to " + peer)
                    pass
                except requests.exceptions.ConnectionError:
                    logger.debug("Couldn't connect to " + peer)
                    pass
                except Exception as e:
                    handle_exception(e)
                    pass
                if status_code in [201, 503]:
                    if status_code == 201:
                        logger.info("Adding peer " + str(peer))
                        peer_addr = response.text
                        self.register_peer(peer, peer_addr)
                        successful_adds += 1
                    if status_code == 503:
                        logger.info("Peer was at peer-maximum")

        return successful_adds

    def join_network_worker(self):
        # Sleeping random amount to not have seed-clash (cannot do circular
        #  adding of peers at the exact same time as seeds)
        logger.debug("Running on port " + str(self.port))
        sleeptime = 2 + random.randrange(3000) / 1000.0
        logger.debug("Sleeping for " + str(int(sleeptime))
                     + "s before network join")
        time.sleep(sleeptime)

        # First try to add seeds
        if self.port < 5003:
            self.send_mutual_add_requests(config['seeds'])

        # Then get random sample of peers from them
        peer_samples = self.get_sample_of_peers_from(config['seeds'])

        # Then add those peers
        self.send_mutual_add_requests(peer_samples)

        # TODO: Sync latest datastructures with peers (choosing the majority?)

        # Try to add new peers until my peerlist is at full capacity
        while True:
            time.sleep(4)
            if len(self.peers) < config['min_peers']:
                logger.debug("peerlist below minimum, trying to add more peers")
                peer_samples = self.get_sample_of_peers_from(self.peers)
                self.send_mutual_add_requests(peer_samples)
                self.ready = False
            else:
                self.ready = True
            if len(self.peers) < 1:
                logger.debug("no peers! adding seeds again")
                peer_samples = self.get_sample_of_peers_from(config['seeds'])
                self.send_mutual_add_requests(peer_samples)
