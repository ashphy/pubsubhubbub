#!/usr/bin/env python
#
# Copyright 2008 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""PubSubHubbub protocol Hub implementation built on Google App Engine.

=== Model classes:

* Subscription: A single subscriber's lease on a topic URL. Also represents a
  work item of a subscription that is awaiting confirmation (sub. or unsub).

* FeedToFetch: Work item inserted when a publish event occurs. This will be
  moved to the Task Queue API once available.

* KnownFeed: Materialized view of all distinct topic URLs. Written by
  background task every time a new subscription is made for a topic URL.
  Used for mapping from input topic URLs to feed IDs and then back to topic
  URLs, to properly handle any feed aliases. Also used for doing bootstrap
  polling of feeds.

* KnownFeedIdentity: Reverse index of feed ID to topic URLs. Used in
  conjunction with KnownFeed to properly canonicalize feed aliases on
  subscription and pinging.

* KnownFeedStats: Statistics about a topic URL. Used to provide subscriber
  counts to publishers on feed fetch.

* FeedRecord: Metadata information about a feed, the last time it was polled,
  and any headers that may affect future polling. Also contains any debugging
  information about the last feed fetch and why it may have failed.

* FeedEntryRecord: Record of a single entry in a single feed. May eventually
  be garbage collected after enough time has passed since it was last seen.

* EventToDeliver: Work item that contains the content to deliver for a feed
  event. Maintains current position in subscribers and number of delivery
  failures. Used to coordinate delivery retries. Will be deleted in successful
  cases or stick around in the event of complete failures for debugging.

* PollingMarker: Work item that keeps track of the last time all KnownFeed
  instances were fetched. Used to do bootstrap polling.


=== Entity groups:

Subscription entities are in their own entity group to allow for a high number
of simultaneous subscriptions for the same topic URL. FeedToFetch is also in
its own entity group for the same reason. FeedRecord, FeedEntryRecord, and
EventToDeliver entries are all in the same entity group, however, to ensure that
each feed polling is either full committed and delivered to subscribers or fails
and will be retried at a later time.

                  ------------
                 | FeedRecord |
                  -----+------
                       |
                       |
         +-------------+-------------+
         |                           |
         |                           |
 --------+--------           --------+-------
| FeedEntryRecord |         | EventToDeliver |
 -----------------           ----------------
"""

# Bigger TODOs (in priority order)
#
# - Improve polling algorithm to keep stats on each feed.
#
# - Do not poll a feed if we've gotten an event from the publisher in less
#   than the polling period.

import datetime
import gc
import hashlib
import hmac
import logging
import os
import random
import sgmllib
import time
import traceback
import urllib
import urlparse
import wsgiref.handlers
import xml.sax

from google.appengine import runtime
from google.appengine.api import datastore_types
from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.api import urlfetch_errors
from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.runtime import apiproxy_errors

import async_apiproxy
import dos
import feed_diff
import feed_identifier
import fork_join_queue
import urlfetch_async

import mapreduce.control
import mapreduce.model

import webapp2

async_proxy = async_apiproxy.AsyncAPIProxy()

################################################################################
# Config parameters

DEBUG = True

if DEBUG:
  logging.getLogger().setLevel(logging.DEBUG)

# How many subscribers to contact at a time when delivering events.
EVENT_SUBSCRIBER_CHUNK_SIZE = 50

# Maximum number of times to attempt a subscription retry.
MAX_SUBSCRIPTION_CONFIRM_FAILURES = 4

# Period to use for exponential backoff on subscription confirm retries.
SUBSCRIPTION_RETRY_PERIOD = 30 # seconds

# Maximum number of times to attempt to pull a feed.
MAX_FEED_PULL_FAILURES = 4

# Period to use for exponential backoff on feed pulling.
FEED_PULL_RETRY_PERIOD = 30 # seconds

# Maximum number of times to attempt to deliver a feed event.
MAX_DELIVERY_FAILURES = 4

# Period to use for exponential backoff on feed event delivery.
DELIVERY_RETRY_PERIOD = 30 # seconds

# Period at which feed IDs should be refreshed.
FEED_IDENTITY_UPDATE_PERIOD = (20 * 24 * 60 * 60) # 20 days

# Number of polling feeds to fetch from the Datastore at a time.
BOOSTRAP_FEED_CHUNK_SIZE = 50

# How many old Subscription instances to clean up at a time.
SUBSCRIPTION_CLEANUP_CHUNK_SIZE = 100

# How far before expiration to refresh subscriptions.
SUBSCRIPTION_CHECK_BUFFER_SECONDS = (24 * 60 * 60)  # 24 hours

# How many mapper shards to use for reconfirming subscriptions.
SUBSCRIPTION_RECONFIRM_SHARD_COUNT = 4

# How often to poll feeds.
POLLING_BOOTSTRAP_PERIOD = 10800  # in seconds; 3 hours

# Default expiration time of a lease.
DEFAULT_LEASE_SECONDS = (5 * 24 * 60 * 60)  # 5 days

# Maximum expiration time of a lease.
MAX_LEASE_SECONDS = (10 * 24 * 60 * 60)  # 10 days

# Maximum number of redirects to follow when feed fetching.
MAX_REDIRECTS = 7

# Maximum time to wait for fetching a feed in seconds.
MAX_FETCH_SECONDS = 10

# Number of times to try to split FeedEntryRecord, EventToDeliver, and
# FeedRecord entities when putting them and their size is too large.
PUT_SPLITTING_ATTEMPTS = 10

# Maximum number of FeedEntryRecord entries to look up in parallel.
MAX_FEED_ENTRY_RECORD_LOOKUPS = 500

# Maximum number of FeedEntryRecord entries to save at the same time when
# a new EventToDeliver is being written.
MAX_FEED_RECORD_SAVES = 100

# Maximum number of new FeedEntryRecords to process and insert at a time. Any
# remaining will be split into another EventToDeliver instance.
MAX_NEW_FEED_ENTRY_RECORDS = 200

################################################################################
# URL scoring Parameters

# Fetching feeds
FETCH_SCORER = dos.UrlScorer(
  period=300,  # Seconds
  min_requests=5,  # per second
  max_failure_percentage=0.8,
  prefix='pull_feed')

# Pushing events
DELIVERY_SCORER = dos.UrlScorer(
  period=300,  # Seconds
  min_requests=0.5,  # per second
  max_failure_percentage=0.8,
  prefix='deliver_events')


################################################################################
# Fetching samplers

FETCH_URL_SAMPLE_MINUTE = dos.ReservoirConfig(
    'fetch_url_1m',
    period=60,
    samples=10000,
    by_url=True,
    value_units='% errors')

FETCH_URL_SAMPLE_30_MINUTE = dos.ReservoirConfig(
    'fetch_url_30m',
    period=1800,
    samples=10000,
    by_url=True,
    value_units='% errors')

FETCH_URL_SAMPLE_HOUR = dos.ReservoirConfig(
    'fetch_url_1h',
    period=3600,
    samples=10000,
    by_url=True,
    value_units='% errors')

FETCH_URL_SAMPLE_DAY = dos.ReservoirConfig(
    'fetch_url_1d',
    period=86400,
    samples=10000,
    by_url=True,
    value_units='% errors')

FETCH_DOMAIN_SAMPLE_MINUTE = dos.ReservoirConfig(
    'fetch_domain_1m',
    period=60,
    samples=10000,
    by_domain=True,
    value_units='% errors')

FETCH_DOMAIN_SAMPLE_30_MINUTE = dos.ReservoirConfig(
    'fetch_domain_30m',
    period=1800,
    samples=10000,
    by_domain=True,
    value_units='% errors')

FETCH_DOMAIN_SAMPLE_HOUR = dos.ReservoirConfig(
    'fetch_domain_1h',
    period=3600,
    samples=10000,
    by_domain=True,
    value_units='% errors')

FETCH_DOMAIN_SAMPLE_DAY = dos.ReservoirConfig(
    'fetch_domain_1d',
    period=86400,
    samples=10000,
    by_domain=True,
    value_units='% errors')

FETCH_URL_SAMPLE_MINUTE_LATENCY = dos.ReservoirConfig(
    'fetch_url_1m_latency',
    period=60,
    samples=10000,
    by_url=True,
    value_units='ms')

FETCH_URL_SAMPLE_30_MINUTE_LATENCY = dos.ReservoirConfig(
    'fetch_url_30m_latency',
    period=1800,
    samples=10000,
    by_url=True,
    value_units='ms')

FETCH_URL_SAMPLE_HOUR_LATENCY = dos.ReservoirConfig(
    'fetch_url_1h_latency',
    period=3600,
    samples=10000,
    by_url=True,
    value_units='ms')

FETCH_URL_SAMPLE_DAY_LATENCY = dos.ReservoirConfig(
    'fetch_url_1d_latency',
    period=86400,
    samples=10000,
    by_url=True,
    value_units='ms')

FETCH_DOMAIN_SAMPLE_MINUTE_LATENCY = dos.ReservoirConfig(
    'fetch_domain_1m_latency',
    period=60,
    samples=10000,
    by_domain=True,
    value_units='ms')

FETCH_DOMAIN_SAMPLE_30_MINUTE_LATENCY = dos.ReservoirConfig(
    'fetch_domain_30m_latency',
    period=1800,
    samples=10000,
    by_domain=True,
    value_units='ms')

FETCH_DOMAIN_SAMPLE_HOUR_LATENCY = dos.ReservoirConfig(
    'fetch_domain_1h_latency',
    period=3600,
    samples=10000,
    by_domain=True,
    value_units='ms')

FETCH_DOMAIN_SAMPLE_DAY_LATENCY = dos.ReservoirConfig(
    'fetch_domain_1d_latency',
    period=86400,
    samples=10000,
    by_domain=True,
    value_units='ms')


def report_fetch(reporter, url, success, latency):
  """Reports statistics information for a feed fetch.

  Args:
    reporter: dos.Reporter instance.
    url: The URL of the topic URL that was fetched.
    success: True if the fetch was successful, False otherwise.
    latency: End-to-end fetch latency in milliseconds.
  """
  value = 100 * int(not success)
  reporter.set(url, FETCH_URL_SAMPLE_MINUTE, value)
  reporter.set(url, FETCH_URL_SAMPLE_30_MINUTE, value)
  reporter.set(url, FETCH_URL_SAMPLE_HOUR, value)
  reporter.set(url, FETCH_URL_SAMPLE_DAY, value)
  reporter.set(url, FETCH_DOMAIN_SAMPLE_MINUTE, value)
  reporter.set(url, FETCH_DOMAIN_SAMPLE_30_MINUTE, value)
  reporter.set(url, FETCH_DOMAIN_SAMPLE_HOUR, value)
  reporter.set(url, FETCH_DOMAIN_SAMPLE_DAY, value)
  reporter.set(url, FETCH_URL_SAMPLE_MINUTE_LATENCY, latency)
  reporter.set(url, FETCH_URL_SAMPLE_30_MINUTE_LATENCY, latency)
  reporter.set(url, FETCH_URL_SAMPLE_HOUR_LATENCY, latency)
  reporter.set(url, FETCH_URL_SAMPLE_DAY_LATENCY, latency)
  reporter.set(url, FETCH_DOMAIN_SAMPLE_MINUTE_LATENCY, latency)
  reporter.set(url, FETCH_DOMAIN_SAMPLE_30_MINUTE_LATENCY, latency)
  reporter.set(url, FETCH_DOMAIN_SAMPLE_HOUR_LATENCY, latency)
  reporter.set(url, FETCH_DOMAIN_SAMPLE_DAY_LATENCY, latency)


FETCH_SAMPLER = dos.MultiSampler([
    FETCH_URL_SAMPLE_MINUTE,
    FETCH_URL_SAMPLE_30_MINUTE,
    FETCH_URL_SAMPLE_HOUR,
    FETCH_URL_SAMPLE_DAY,
    FETCH_DOMAIN_SAMPLE_MINUTE,
    FETCH_DOMAIN_SAMPLE_30_MINUTE,
    FETCH_DOMAIN_SAMPLE_HOUR,
    FETCH_DOMAIN_SAMPLE_DAY,
    FETCH_URL_SAMPLE_MINUTE_LATENCY,
    FETCH_URL_SAMPLE_30_MINUTE_LATENCY,
    FETCH_URL_SAMPLE_HOUR_LATENCY,
    FETCH_URL_SAMPLE_DAY_LATENCY,
    FETCH_DOMAIN_SAMPLE_MINUTE_LATENCY,
    FETCH_DOMAIN_SAMPLE_30_MINUTE_LATENCY,
    FETCH_DOMAIN_SAMPLE_HOUR_LATENCY,
    FETCH_DOMAIN_SAMPLE_DAY_LATENCY,
])

################################################################################
# Delivery samplers

DELIVERY_URL_SAMPLE_MINUTE = dos.ReservoirConfig(
    'delivery_url_1m',
    period=60,
    samples=10000,
    by_url=True,
    value_units='% errors')

DELIVERY_URL_SAMPLE_30_MINUTE = dos.ReservoirConfig(
    'delivery_url_30m',
    period=1800,
    samples=10000,
    by_url=True,
    value_units='% errors')

DELIVERY_URL_SAMPLE_HOUR = dos.ReservoirConfig(
    'delivery_url_1h',
    period=3600,
    samples=10000,
    by_url=True,
    value_units='% errors')

DELIVERY_URL_SAMPLE_DAY = dos.ReservoirConfig(
    'delivery_url_1d',
    period=86400,
    samples=10000,
    by_url=True,
    value_units='% errors')

DELIVERY_DOMAIN_SAMPLE_MINUTE = dos.ReservoirConfig(
    'delivery_domain_1m',
    period=60,
    samples=10000,
    by_domain=True,
    value_units='% errors')

DELIVERY_DOMAIN_SAMPLE_30_MINUTE = dos.ReservoirConfig(
    'delivery_domain_30m',
    period=1800,
    samples=10000,
    by_domain=True,
    value_units='% errors')

DELIVERY_DOMAIN_SAMPLE_HOUR = dos.ReservoirConfig(
    'delivery_domain_1h',
    period=3600,
    samples=10000,
    by_domain=True,
    value_units='% errors')

DELIVERY_DOMAIN_SAMPLE_DAY = dos.ReservoirConfig(
    'delivery_domain_1d',
    period=86400,
    samples=10000,
    by_domain=True,
    value_units='% errors')

DELIVERY_URL_SAMPLE_MINUTE_LATENCY = dos.ReservoirConfig(
    'delivery_url_1m_latency',
    period=60,
    samples=10000,
    by_url=True,
    value_units='ms')

DELIVERY_URL_SAMPLE_30_MINUTE_LATENCY = dos.ReservoirConfig(
    'delivery_url_30m_latency',
    period=1800,
    samples=10000,
    by_url=True,
    value_units='ms')

DELIVERY_URL_SAMPLE_HOUR_LATENCY = dos.ReservoirConfig(
    'delivery_url_1h_latency',
    period=3600,
    samples=10000,
    by_url=True,
    value_units='ms')

DELIVERY_URL_SAMPLE_DAY_LATENCY = dos.ReservoirConfig(
    'delivery_url_1d_latency',
    period=86400,
    samples=10000,
    by_url=True,
    value_units='ms')

DELIVERY_DOMAIN_SAMPLE_MINUTE_LATENCY = dos.ReservoirConfig(
    'delivery_domain_1m_latency',
    period=60,
    samples=10000,
    by_domain=True,
    value_units='ms')

DELIVERY_DOMAIN_SAMPLE_30_MINUTE_LATENCY = dos.ReservoirConfig(
    'delivery_domain_30m_latency',
    period=1800,
    samples=10000,
    by_domain=True,
    value_units='ms')

DELIVERY_DOMAIN_SAMPLE_HOUR_LATENCY = dos.ReservoirConfig(
    'delivery_domain_1h_latency',
    period=3600,
    samples=10000,
    by_domain=True,
    value_units='ms')

DELIVERY_DOMAIN_SAMPLE_DAY_LATENCY = dos.ReservoirConfig(
    'delivery_domain_1d_latency',
    period=86400,
    samples=10000,
    by_domain=True,
    value_units='ms')


def report_delivery(reporter, url, success, latency):
  """Reports statistics information for a event delivery to a callback.

  Args:
    reporter: dos.Reporter instance.
    url: The URL of the callback that received the event.
    success: True if the delivery was successful, False otherwise.
    latency: End-to-end fetch latency in milliseconds.
  """
  value = 100 * int(not success)
  reporter.set(url, DELIVERY_URL_SAMPLE_MINUTE, value)
  reporter.set(url, DELIVERY_URL_SAMPLE_30_MINUTE, value)
  reporter.set(url, DELIVERY_URL_SAMPLE_HOUR, value)
  reporter.set(url, DELIVERY_URL_SAMPLE_DAY, value)
  reporter.set(url, DELIVERY_DOMAIN_SAMPLE_MINUTE, value)
  reporter.set(url, DELIVERY_DOMAIN_SAMPLE_30_MINUTE, value)
  reporter.set(url, DELIVERY_DOMAIN_SAMPLE_HOUR, value)
  reporter.set(url, DELIVERY_DOMAIN_SAMPLE_DAY, value)
  reporter.set(url, DELIVERY_URL_SAMPLE_MINUTE_LATENCY, latency)
  reporter.set(url, DELIVERY_URL_SAMPLE_30_MINUTE_LATENCY, latency)
  reporter.set(url, DELIVERY_URL_SAMPLE_HOUR_LATENCY, latency)
  reporter.set(url, DELIVERY_URL_SAMPLE_DAY_LATENCY, latency)
  reporter.set(url, DELIVERY_DOMAIN_SAMPLE_MINUTE_LATENCY, latency)
  reporter.set(url, DELIVERY_DOMAIN_SAMPLE_30_MINUTE_LATENCY, latency)
  reporter.set(url, DELIVERY_DOMAIN_SAMPLE_HOUR_LATENCY, latency)
  reporter.set(url, DELIVERY_DOMAIN_SAMPLE_DAY_LATENCY, latency)


DELIVERY_SAMPLER = dos.MultiSampler([
    DELIVERY_URL_SAMPLE_MINUTE,
    DELIVERY_URL_SAMPLE_30_MINUTE,
    DELIVERY_URL_SAMPLE_HOUR,
    DELIVERY_URL_SAMPLE_DAY,
    DELIVERY_DOMAIN_SAMPLE_MINUTE,
    DELIVERY_DOMAIN_SAMPLE_30_MINUTE,
    DELIVERY_DOMAIN_SAMPLE_HOUR,
    DELIVERY_DOMAIN_SAMPLE_DAY,
    DELIVERY_URL_SAMPLE_MINUTE_LATENCY,
    DELIVERY_URL_SAMPLE_30_MINUTE_LATENCY,
    DELIVERY_URL_SAMPLE_HOUR_LATENCY,
    DELIVERY_URL_SAMPLE_DAY_LATENCY,
    DELIVERY_DOMAIN_SAMPLE_MINUTE_LATENCY,
    DELIVERY_DOMAIN_SAMPLE_30_MINUTE_LATENCY,
    DELIVERY_DOMAIN_SAMPLE_HOUR_LATENCY,
    DELIVERY_DOMAIN_SAMPLE_DAY_LATENCY,
])

################################################################################
# Constants

ATOM = 'atom'
RSS = 'rss'
ARBITRARY = 'arbitrary'

VALID_PORTS = frozenset([
    '80', '443', '4443', '8080', '8081', '8082', '8083', '8084', '8085',
    '8086', '8087', '8088', '8089', '8188', '8444', '8990'])

EVENT_QUEUE = 'event-delivery'

EVENT_RETRIES_QUEUE = 'event-delivery-retries'

FEED_QUEUE = 'feed-pulls'

FEED_RETRIES_QUEUE = 'feed-pulls-retries'

POLLING_QUEUE = 'polling'

SUBSCRIPTION_QUEUE = 'subscriptions'

MAPPINGS_QUEUE = 'mappings'

################################################################################
# Helper functions

def utf8encoded(data):
  """Encodes a string as utf-8 data and returns an ascii string.

  Args:
    data: The string data to encode.

  Returns:
    An ascii string, or None if the 'data' parameter was None.
  """
  if data is None:
    return None
  if isinstance(data, unicode):
    return unicode(data).encode('utf-8')
  else:
    return data


def normalize_iri(url):
  """Converts a URL (possibly containing unicode characters) to an IRI.

  Args:
    url: String (normal or unicode) containing a URL, presumably having
      already been percent-decoded by a web framework receiving request
      parameters in a POST body or GET request's URL.

  Returns:
    A properly encoded IRI (see RFC 3987).
  """
  def chr_or_escape(unicode_char):
    if ord(unicode_char) > 0x7f:
      return urllib.quote(unicode_char.encode('utf-8'))
    else:
      return unicode_char
  return ''.join(chr_or_escape(c) for c in unicode(url))


def sha1_hash(value):
  """Returns the sha1 hash of the supplied value."""
  return hashlib.sha1(utf8encoded(value)).hexdigest()


def get_hash_key_name(value):
  """Returns a valid entity key_name that's a hash of the supplied value."""
  return 'hash_' + sha1_hash(value)


def sha1_hmac(secret, data):
  """Returns the sha1 hmac for a chunk of data and a secret."""
  # For Python 2.6, which can only compute hmacs on non-unicode data.
  secret = utf8encoded(secret)
  data = utf8encoded(data)
  return hmac.new(secret, data, hashlib.sha1).hexdigest()


def is_dev_env():
  """Returns True if we're running in the development environment."""
  return 'Dev' in os.environ.get('SERVER_SOFTWARE', '')


def work_queue_only(func):
  """Decorator that only allows a request if from cron job, task, or an admin.

  Also allows access if running in development server environment.

  Args:
    func: A webapp.RequestHandler method.

  Returns:
    Function that will return a 401 error if not from an authorized source.
  """
  def decorated(myself, *args, **kwargs):
    if ('X-AppEngine-Cron' in myself.request.headers or
        'X-AppEngine-TaskName' in myself.request.headers or
        is_dev_env() or users.is_current_user_admin()):
      return func(myself, *args, **kwargs)
    elif users.get_current_user() is None:
      myself.redirect(users.create_login_url(myself.request.url))
    else:
      myself.response.set_status(401)
      myself.response.out.write('Handler only accessible for work queues')
  return decorated


def is_valid_url(url):
  """Returns True if the URL is valid, False otherwise."""
  split = urlparse.urlparse(url)
  if not split.scheme in ('http', 'https'):
    logging.debug('URL scheme is invalid: %s', url)
    return False

  netloc, port = (split.netloc.split(':', 1) + [''])[:2]
  if port and not is_dev_env() and port not in VALID_PORTS:
    logging.debug('URL port is invalid: %s', url)
    return False

  if split.fragment:
    logging.debug('URL includes fragment: %s', url)
    return False

  return True


_VALID_CHARS = (
  'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
  'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z',
  'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm',
  'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z',
  '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '-', '_',
)


def get_random_challenge():
  """Returns a string containing a random challenge token."""
  return ''.join(random.choice(_VALID_CHARS) for i in xrange(128))

################################################################################
# Models

class Subscription(db.Model):
  """Represents a single subscription to a topic for a callback URL."""

  STATE_NOT_VERIFIED = 'not_verified'
  STATE_VERIFIED = 'verified'
  STATE_TO_DELETE = 'to_delete'
  STATES = frozenset([
    STATE_NOT_VERIFIED,
    STATE_VERIFIED,
    STATE_TO_DELETE,
  ])

  callback = db.TextProperty(required=True)
  callback_hash = db.StringProperty(required=True)
  topic = db.TextProperty(required=True)
  topic_hash = db.StringProperty(required=True)
  created_time = db.DateTimeProperty(auto_now_add=True)
  last_modified = db.DateTimeProperty(auto_now=True)
  lease_seconds = db.IntegerProperty(default=DEFAULT_LEASE_SECONDS)
  expiration_time = db.DateTimeProperty(required=True)
  eta = db.DateTimeProperty(auto_now_add=True)
  confirm_failures = db.IntegerProperty(default=0)
  verify_token = db.TextProperty()
  secret = db.TextProperty()
  hmac_algorithm = db.TextProperty()
  subscription_state = db.StringProperty(default=STATE_NOT_VERIFIED,
                                         choices=STATES)

  @staticmethod
  def create_key_name(callback, topic):
    """Returns the key name for a Subscription entity.

    Args:
      callback: URL of the callback subscriber.
      topic: URL of the topic being subscribed to.

    Returns:
      String containing the key name for the corresponding Subscription.
    """
    return get_hash_key_name(u'%s\n%s' % (callback, topic))

  @classmethod
  def insert(cls,
             callback,
             topic,
             verify_token,
             secret,
             hash_func='sha1',
             lease_seconds=DEFAULT_LEASE_SECONDS,
             now=datetime.datetime.now):
    """Marks a callback URL as being subscribed to a topic.

    Creates a new subscription if None already exists. Forces any existing,
    pending request (i.e., async) to immediately enter the verified state.

    Args:
      callback: URL that will receive callbacks.
      topic: The topic to subscribe to.
      verify_token: The verification token to use to confirm the
        subscription request.
      secret: Shared secret used for HMACs.
      hash_func: String with the name of the hash function to use for HMACs.
      lease_seconds: Number of seconds the client would like the subscription
        to last before expiring. Must be a number.
      now: Callable that returns the current time as a datetime instance. Used
        for testing

    Returns:
      True if the subscription was newly created, False otherwise.
    """
    key_name = cls.create_key_name(callback, topic)
    now_time = now()
    def txn():
      sub_is_new = False
      sub = cls.get_by_key_name(key_name)
      if sub is None:
        sub_is_new = True
        sub = cls(key_name=key_name,
                  callback=callback,
                  callback_hash=sha1_hash(callback),
                  topic=topic,
                  topic_hash=sha1_hash(topic),
                  verify_token=verify_token,
                  secret=secret,
                  hash_func=hash_func,
                  lease_seconds=lease_seconds,
                  expiration_time=now_time)
      sub.subscription_state = cls.STATE_VERIFIED
      sub.expiration_time = now_time + datetime.timedelta(seconds=lease_seconds)
      sub.confirm_failures = 0
      sub.verify_token = verify_token
      sub.secret = secret
      sub.put()
      return sub_is_new
    return db.run_in_transaction(txn)

  @classmethod
  def request_insert(cls,
                     callback,
                     topic,
                     verify_token,
                     secret,
                     auto_reconfirm=False,
                     hash_func='sha1',
                     lease_seconds=DEFAULT_LEASE_SECONDS,
                     now=datetime.datetime.now):
    """Records that a callback URL needs verification before being subscribed.

    Creates a new subscription request (for asynchronous verification) if None
    already exists. Any existing subscription request will be overridden;
    for instance, if a subscription has already been verified, this method
    will cause it to be reconfirmed.

    Args:
      callback: URL that will receive callbacks.
      topic: The topic to subscribe to.
      verify_token: The verification token to use to confirm the
        subscription request.
      secret: Shared secret used for HMACs.
      auto_reconfirm: True if this task is being run by the auto-reconfirmation
        offline process; False if this is a user-requested task. Defaults
        to False.
      hash_func: String with the name of the hash function to use for HMACs.
      lease_seconds: Number of seconds the client would like the subscription
        to last before expiring. Must be a number.
      now: Callable that returns the current time as a datetime instance. Used
        for testing

    Returns:
      True if the subscription request was newly created, False otherwise.
    """
    key_name = cls.create_key_name(callback, topic)
    def txn():
      sub_is_new = False
      sub = cls.get_by_key_name(key_name)
      if sub is None:
        sub_is_new = True
        sub = cls(key_name=key_name,
                  callback=callback,
                  callback_hash=sha1_hash(callback),
                  topic=topic,
                  topic_hash=sha1_hash(topic),
                  secret=secret,
                  hash_func=hash_func,
                  verify_token=verify_token,
                  lease_seconds=lease_seconds,
                  expiration_time=(
                      now() + datetime.timedelta(seconds=lease_seconds)))
      sub.confirm_failures = 0
      sub.put()
      sub.enqueue_task(cls.STATE_VERIFIED,
                       verify_token,
                       secret=secret,
                       auto_reconfirm=auto_reconfirm)
      return sub_is_new
    return db.run_in_transaction(txn)

  @classmethod
  def remove(cls, callback, topic):
    """Causes a callback URL to no longer be subscribed to a topic.

    If the callback was not already subscribed to the topic, this method
    will do nothing. Otherwise, the subscription will immediately be removed.

    Args:
      callback: URL that will receive callbacks.
      topic: The topic to subscribe to.

    Returns:
      True if the subscription had previously existed, False otherwise.
    """
    key_name = cls.create_key_name(callback, topic)
    def txn():
      sub = cls.get_by_key_name(key_name)
      if sub is not None:
        sub.delete()
        return True
      return False
    return db.run_in_transaction(txn)

  @classmethod
  def request_remove(cls, callback, topic, verify_token):
    """Records that a callback URL needs to be unsubscribed.

    Creates a new request to unsubscribe a callback URL from a topic (where
    verification should happen asynchronously). If an unsubscribe request
    has already been made, this method will do nothing.

    Args:
      callback: URL that will receive callbacks.
      topic: The topic to subscribe to.
      verify_token: The verification token to use to confirm the
        unsubscription request.

    Returns:
      True if the Subscription to remove actually exists, False otherwise.
    """
    key_name = cls.create_key_name(callback, topic)
    def txn():
      sub = cls.get_by_key_name(key_name)
      if sub is not None:
        sub.confirm_failures = 0
        sub.put()
        sub.enqueue_task(cls.STATE_TO_DELETE, verify_token)
        return True
      else:
        return False
    return db.run_in_transaction(txn)

  @classmethod
  def archive(cls, callback, topic):
    """Archives a subscription as no longer active.

    Args:
      callback: URL that will receive callbacks.
      topic: The topic to subscribe to.
    """
    key_name = cls.create_key_name(callback, topic)
    def txn():
      sub = cls.get_by_key_name(key_name)
      if sub is not None:
        sub.subscription_state = cls.STATE_TO_DELETE
        sub.confirm_failures = 0
        sub.put()
    return db.run_in_transaction(txn)

  @classmethod
  def has_subscribers(cls, topic):
    """Check if a topic URL has verified subscribers.

    Args:
      topic: The topic URL to check for subscribers.

    Returns:
      True if it has verified subscribers, False otherwise.
    """
    if (cls.all(keys_only=True).filter('topic_hash =', sha1_hash(topic))
        .filter('subscription_state =', cls.STATE_VERIFIED).get() is not None):
      return True
    else:
      return False

  @classmethod
  def get_subscribers(cls, topic, count, starting_at_callback=None):
    """Gets the list of subscribers starting at an offset.

    Args:
      topic: The topic URL to retrieve subscribers for.
      count: How many subscribers to retrieve.
      starting_at_callback: A string containing the callback hash to offset
        to when retrieving more subscribers. The callback at the given offset
        *will* be included in the results. If None, then subscribers will
        be retrieved from the beginning.

    Returns:
      List of Subscription objects that were found, or an empty list if none
      were found.
    """
    query = cls.all()
    query.filter('topic_hash =', sha1_hash(topic))
    query.filter('subscription_state = ', cls.STATE_VERIFIED)
    if starting_at_callback:
      query.filter('callback_hash >=', sha1_hash(starting_at_callback))
    query.order('callback_hash')

    return query.fetch(count)

  def enqueue_task(self,
                   next_state,
                   verify_token,
                   auto_reconfirm=False,
                   secret=None):
    """Enqueues a task to confirm this Subscription.

    Args:
      next_state: The next state this subscription should be in.
      verify_token: The verify_token to use when confirming this request.
      auto_reconfirm: True if this task is being run by the auto-reconfirmation
        offline process; False if this is a user-requested task. Defaults
        to False.
      secret: Only required for subscription confirmation (not unsubscribe).
        The new secret to use for this subscription after successful
        confirmation.
    """
    RETRIES = 3
    if auto_reconfirm:
      target_queue = POLLING_QUEUE
    else:
      target_queue = SUBSCRIPTION_QUEUE
    for i in xrange(RETRIES):
      try:
        taskqueue.Task(
            url='/work/subscriptions',
            eta=self.eta,
            params={'subscription_key_name': self.key().name(),
                    'next_state': next_state,
                    'verify_token': verify_token,
                    'secret': secret or '',
                    'auto_reconfirm': str(auto_reconfirm)}
            ).add(target_queue, transactional=True)
      except (taskqueue.Error, apiproxy_errors.Error):
        logging.exception('Could not insert task to confirm '
                          'topic = %s, callback = %s',
                          self.topic, self.callback)
        if i == (RETRIES - 1):
          raise
      else:
        return

  def confirm_failed(self,
                     next_state,
                     verify_token,
                     auto_reconfirm=False,
                     secret=None,
                     max_failures=MAX_SUBSCRIPTION_CONFIRM_FAILURES,
                     retry_period=SUBSCRIPTION_RETRY_PERIOD,
                     now=datetime.datetime.utcnow):
    """Reports that an asynchronous confirmation request has failed.

    This will delete this entity if the maximum number of failures has been
    exceeded.

    Args:
      next_state: The next state this subscription should be in.
      verify_token: The verify_token to use when confirming this request.
      auto_reconfirm: True if this task is being run by the auto-reconfirmation
        offline process; False if this is a user-requested task.
      secret: The new secret to use for this subscription after successful
        confirmation.
      max_failures: Maximum failures to allow before giving up.
      retry_period: Initial period for doing exponential (base-2) backoff.
      now: Returns the current time as a UTC datetime.

    Returns:
      True if this Subscription confirmation should be retried again. Returns
      False if we should give up and never try again.
    """
    def txn():
      if self.confirm_failures >= max_failures:
        logging.debug('Max subscription failures exceeded, giving up.')
        return False
      else:
        retry_delay = retry_period * (2 ** self.confirm_failures)
        self.eta = now() + datetime.timedelta(seconds=retry_delay)
        self.confirm_failures += 1
      self.put()
      self.enqueue_task(next_state,
                        verify_token,
                        auto_reconfirm=auto_reconfirm,
                        secret=secret)
      return True
    return db.run_in_transaction(txn)


class FeedToFetch(db.Expando):
  """A feed that has new data that needs to be pulled.

  The key name of this entity is a get_hash_key_name() hash of the topic URL, so
  multiple inserts will only ever write a single entity.
  """

  topic = db.TextProperty(required=True)
  eta = db.DateTimeProperty(auto_now_add=True, indexed=False)
  fetching_failures = db.IntegerProperty(default=0, indexed=False)
  totally_failed = db.BooleanProperty(default=False, indexed=False)
  source_keys = db.StringListProperty(indexed=False)
  source_values = db.StringListProperty(indexed=False)
  work_index = db.IntegerProperty()

  # TODO(bslatkin): Add fetching failure reason (urlfetch, parsing, etc) and
  # surface it on the topic details page.

  FORK_JOIN_QUEUE = None

  @classmethod
  def get_by_topic(cls, topic):
    """Retrives a FeedToFetch by the topic URL.

    Args:
      topic: The URL for the feed.

    Returns:
      The FeedToFetch or None if it does not exist.
    """
    return cls.get_by_key_name(get_hash_key_name(topic))

  @classmethod
  def insert(cls, topic_list, source_dict=None, memory_only=True):
    """Inserts a set of FeedToFetch entities for a set of topics.

    Overwrites any existing entities that are already there.

    Args:
      topic_list: List of the topic URLs of feeds that need to be fetched.
      source_dict: Dictionary of sources for the feed. Defaults to an empty
        dictionary.
      memory_only: Only save FeedToFetch records to memory, not to disk.

    Returns:
      The list of FeedToFetch records that was created.
    """
    if not topic_list:
      return

    if source_dict:
      source_keys, source_values = zip(*source_dict.items())  # Yay Python!
    else:
      source_keys, source_values = [], []

    if os.environ.get('HTTP_X_APPENGINE_QUEUENAME') == POLLING_QUEUE:
      cls.FORK_JOIN_QUEUE.queue_name = POLLING_QUEUE
    else:
      cls.FORK_JOIN_QUEUE.queue_name = FEED_QUEUE

    if memory_only:
      work_index = cls.FORK_JOIN_QUEUE.next_index()
    else:
      work_index = None
    try:
      feed_list = [
          cls(key=db.Key.from_path(cls.kind(), get_hash_key_name(topic)),
              topic=topic,
              source_keys=list(source_keys),
              source_values=list(source_values),
              work_index=work_index)
          for topic in set(topic_list)]
      if memory_only:
        cls.FORK_JOIN_QUEUE.put(work_index, feed_list)
      else:
        # TODO(bslatkin): Insert fetching tasks here to fix the polling
        # mode for this codebase.
        db.put(feed_list)
    finally:
      if memory_only:
        cls.FORK_JOIN_QUEUE.add(work_index)

    return feed_list

  def fetch_failed(self,
                   max_failures=MAX_FEED_PULL_FAILURES,
                   retry_period=FEED_PULL_RETRY_PERIOD,
                   now=datetime.datetime.utcnow):
    """Reports that feed fetching failed.

    This will mark this feed as failing to fetch. This feed will not be
    refetched until insert() is called again.

    Args:
      max_failures: Maximum failures to allow before giving up.
      retry_period: Initial period for doing exponential (base-2) backoff.
      now: Returns the current time as a UTC datetime.
    """
    orig_failures = self.fetching_failures
    def txn():
      if self.fetching_failures >= max_failures:
        logging.debug('Max fetching failures exceeded, giving up.')
        self.totally_failed = True
      else:
        retry_delay = retry_period * (2 ** orig_failures)
        logging.debug('Fetching failed. Will retry in %s seconds',
                      retry_delay)
        self.eta = now() + datetime.timedelta(seconds=retry_delay)
        self.fetching_failures = orig_failures + 1
        self._enqueue_retry_task()
      self.put()
    try:
      db.run_in_transaction_custom_retries(2, txn)
    except:
      logging.exception('Could not mark feed fetching as a failure: topic=%r',
                        self.topic)

  def done(self):
    """The feed fetch has completed successfully.

    This will delete this FeedToFetch entity iff the ETA has not changed,
    meaning a subsequent publish event did not happen for this topic URL. If
    the ETA has changed, then we can safely assume there is a pending Task to
    take care of this FeedToFetch and we should leave the entry.

    Returns:
      True if the entity was deleted, False otherwise. In the case the
      FeedToFetch record never made it into the Datastore (because it only
      ever lived in the in-memory cache), this function will return False.
    """
    def txn():
      other = db.get(self.key())
      if other and other.eta == self.eta:
        other.delete()
        return True
      else:
        return False
    return db.run_in_transaction(txn)

  def _enqueue_retry_task(self):
    """Enqueues a task to retry fetching this feed."""
    RETRIES = 3

    if os.environ.get('HTTP_X_APPENGINE_QUEUENAME') == POLLING_QUEUE:
      queue_name = POLLING_QUEUE
    else:
      queue_name = FEED_RETRIES_QUEUE

    for i in xrange(RETRIES):
      try:
        taskqueue.Task(
            url='/work/pull_feeds',
            eta=self.eta,
            params={'topic': self.topic}).add(queue_name, transactional=True)
      except (taskqueue.Error, apiproxy_errors.Error):
        if i == (RETRIES - 1):
          raise
      else:
        return


FeedToFetch.FORK_JOIN_QUEUE = fork_join_queue.MemcacheForkJoinQueue(
    FeedToFetch,
    FeedToFetch.work_index,
    '/work/pull_feeds',
    FEED_QUEUE,
    batch_size=15,
    batch_period_ms=500,
    lock_timeout_ms=10000,
    sync_timeout_ms=250,
    stall_timeout_ms=30000,
    acquire_timeout_ms=10,
    acquire_attempts=50,
    shard_count=1,
    expiration_seconds=600)  # Give up on fetches after 10 minutes.


class FeedRecord(db.Model):
  """Represents record of the feed from when it has been polled.

  This contains everything in a feed except for the entry data. That means any
  footers, top-level XML elements, namespace declarations, etc, will be
  captured in this entity.

  The key name of this entity is a get_hash_key_name() of the topic URL.
  """

  topic = db.TextProperty(required=True)
  header_footer = db.TextProperty()
  last_updated = db.DateTimeProperty(auto_now=True, indexed=False)
  format = db.TextProperty()  # 'atom', 'rss', or 'arbitrary'

  # Content-related headers served by the feed's host.
  content_type = db.TextProperty()
  last_modified = db.TextProperty()
  etag = db.TextProperty()

  @staticmethod
  def create_key_name(topic):
    """Creates a key name for a FeedRecord for a topic.

    Args:
      topic: The topic URL for the FeedRecord.

    Returns:
      String containing the key name.
    """
    return get_hash_key_name(topic)

  @classmethod
  def get_or_create_all(cls, topic_list):
    """Retrieves and/or creates FeedRecord entities for the supplied topics.

    Args:
      topic_list: List of topics to retrieve.

    Returns:
      The list of FeedRecords corresponding to the input topic list in the
      same order they were supplied.
    """
    key_list = [db.Key.from_path(cls.kind(), cls.create_key_name(t))
                for t in topic_list]
    found_list = db.get(key_list)
    results = []
    for topic, key, found in zip(topic_list, key_list, found_list):
      if found:
        results.append(found)
      else:
        results.append(cls(key=key, topic=topic))
    return results

  @classmethod
  def get_or_create(cls, topic):
    """Retrieves a FeedRecord by its topic or creates it if non-existent.

    Args:
      topic: The topic URL to retrieve the FeedRecord for.

    Returns:
      The FeedRecord found for this topic or a new one if it did not already
      exist.
    """
    return cls.get_or_insert(FeedRecord.create_key_name(topic), topic=topic)

  def update(self, headers, header_footer=None, format=None):
    """Updates the polling record of this feed.

    This method will *not* insert this instance into the Datastore.

    Args:
      headers: Dictionary of response headers from the feed that should be used
        to determine how to poll the feed in the future.
      header_footer: Contents of the feed's XML document minus the entry data.
        if not supplied, the old value will remain. Only saved for feeds.
      format: The last parsing format that worked correctly for this feed.
        Should be 'rss', 'atom', or 'arbitrary'.
    """
    try:
      self.content_type = headers.get('Content-Type', '').lower()
    except UnicodeDecodeError:
      logging.exception('Content-Type header had bad encoding')

    try:
      self.last_modified = headers.get('Last-Modified')
    except UnicodeDecodeError:
      logging.exception('Last-Modified header had bad encoding')

    try:
      self.etag = headers.get('ETag')
    except UnicodeDecodeError:
      logging.exception('ETag header had bad encoding')

    if format is not None:
      self.format = format
    if header_footer is not None and self.format != ARBITRARY:
      self.header_footer = header_footer

  def get_request_headers(self, subscriber_count):
    """Returns the request headers that should be used to pull this feed.

    Args:
      subscriber_count: The number of subscribers this feed has.

    Returns:
      Dictionary of request header values.
    """
    headers = {
      'Cache-Control': 'no-cache no-store max-age=1',
      'Connection': 'cache-control',
      'Accept': '*/*',
    }
    if self.last_modified:
      headers['If-Modified-Since'] = self.last_modified
    if self.etag:
      headers['If-None-Match'] = self.etag
    if subscriber_count:
      headers['User-Agent'] = (
          'Public Hub (+http://pubsubhubbub.appspot.com; %d subscribers)' %
          subscriber_count)
    return headers


class FeedEntryRecord(db.Expando):
  """Represents a feed entry that has been seen.

  The key name of this entity is a get_hash_key_name() hash of the entry_id.
  """
  entry_content_hash = db.StringProperty(indexed=False)
  update_time = db.DateTimeProperty(auto_now=True, indexed=False)

  @property
  def id_hash(self):
    """Returns the sha1 hash of the entry ID."""
    return self.key().name()[len('hash_'):]

  @classmethod
  def create_key(cls, topic, entry_id):
    """Creates a new Key for a FeedEntryRecord entity.

    Args:
      topic: The topic URL to retrieve entries for.
      entry_id: String containing the entry_id.

    Returns:
      Key instance for this FeedEntryRecord.
    """
    return db.Key.from_path(
        FeedRecord.kind(),
        FeedRecord.create_key_name(topic),
        cls.kind(),
        get_hash_key_name(entry_id))

  @classmethod
  def get_entries_for_topic(cls, topic, entry_id_list):
    """Gets multiple FeedEntryRecord entities for a topic by their entry_ids.

    Args:
      topic: The topic URL to retrieve entries for.
      entry_id_list: Sequence of entry_ids to retrieve.

    Returns:
      List of FeedEntryRecords that were found, if any.
    """
    results = cls.get([cls.create_key(topic, entry_id)
                       for entry_id in entry_id_list])
    # Filter out those pesky Nones.
    return [r for r in results if r]

  @classmethod
  def create_entry_for_topic(cls, topic, entry_id, content_hash):
    """Creates multiple FeedEntryRecords entities for a topic.

    Does not actually insert the entities into the Datastore. This is left to
    the caller so they can do it as part of a larger batch put().

    Args:
      topic: The topic URL to insert entities for.
      entry_id: String containing the ID of the entry.
      content_hash: Sha1 hash of the entry's entire XML content. For example,
        with Atom this would apply to everything from <entry> to </entry> with
        the surrounding tags included. With RSS it would be everything from
        <item> to </item>.

    Returns:
      A new FeedEntryRecord that should be inserted into the Datastore.
    """
    key = cls.create_key(topic, entry_id)
    return cls(key=key, entry_content_hash=content_hash)


class EventToDeliver(db.Expando):
  """Represents a publishing event to deliver to subscribers.

  This model is meant to be used together with Subscription entities. When a
  feed has new published data and needs to be pushed to subscribers, one of
  these entities will be inserted. The background worker should iterate
  through all Subscription entities for this topic, sending them the event
  payload. The update() method should be used to track the progress of the
  background worker as well as any Subscription entities that failed delivery.

  The key_name for each of these entities is unique. It is up to the event
  injection side of the system to de-dupe events to deliver. For example, when
  a publish event comes in, that publish request should be de-duped immediately.
  Later, when the feed puller comes through to grab feed diffs, it should insert
  a single event to deliver, collapsing any overlapping publish events during
  the delay from publish time to feed pulling time.
  """

  DELIVERY_MODES = ('normal', 'retry')
  NORMAL = 'normal'
  RETRY = 'retry'

  topic = db.TextProperty(required=True)
  topic_hash = db.StringProperty(required=True)
  last_callback = db.TextProperty(default='')  # For paging Subscriptions
  failed_callbacks = db.ListProperty(db.Key)  # Refs to Subscription entities
  delivery_mode = db.StringProperty(default=NORMAL, choices=DELIVERY_MODES,
                                    indexed=False)
  retry_attempts = db.IntegerProperty(default=0, indexed=False)
  last_modified = db.DateTimeProperty(required=True, indexed=False)
  totally_failed = db.BooleanProperty(default=False, indexed=False)
  content_type = db.TextProperty(default='')
  max_failures = db.IntegerProperty(indexed=False)

  @classmethod
  def create_event_for_topic(cls,
                             topic,
                             format,
                             content_type,
                             header_footer,
                             entry_payloads,
                             now=datetime.datetime.utcnow,
                             set_parent=True,
                             max_failures=None):
    """Creates an event to deliver for a topic and set of published entries.

    Args:
      topic: The topic that had the event.
      format: Format of the feed, 'atom', 'rss', or 'arbitrary'.
      content_type: The original content type of the feed, fetched from the
        server, if any. May be empty.
      header_footer: The header and footer of the published feed into which
        the entry list will be spliced. For arbitrary content this is the
        full body of the resource.
      entry_payloads: List of strings containing entry payloads (i.e., all
        XML data for each entry, including surrounding tags) in order of newest
        to oldest.
      now: Returns the current time as a UTC datetime. Used in tests.
      set_parent: Set the parent to the FeedRecord for the given topic. This is
        necessary for the parse_feed flow's transaction. Default is True. Set
        to False if this EventToDeliver will be written outside of the
        FeedRecord transaction.
      max_failures: Maximum number of failures to allow for this event. When
        None (the default) it will use the MAX_DELIVERY_FAILURES constant.

    Returns:
      A new EventToDeliver instance that has not been stored.
    """
    if format in (ATOM, RSS):
      # This is feed XML.
      close_index = header_footer.rfind('</')
      assert close_index != -1, 'Could not find "</" in feed envelope'
      end_tag = header_footer[close_index:]
      if 'rss' in end_tag:
        # RSS needs special handling, since it actually closes with
        # a combination of </channel></rss> we need to traverse one
        # level higher.
        close_index = header_footer[:close_index].rfind('</')
        assert close_index != -1, 'Could not find "</channel>" in feed envelope'
        end_tag = header_footer[close_index:]
        content_type = 'application/rss+xml'
      elif 'feed' in end_tag:
        content_type = 'application/atom+xml'
      elif 'rdf' in end_tag:
        content_type = 'application/rdf+xml'

      payload_list = ['<?xml version="1.0" encoding="utf-8"?>',
                      header_footer[:close_index]]
      payload_list.extend(entry_payloads)
      payload_list.append(header_footer[close_index:])
      payload = '\n'.join(payload_list)
    elif format == ARBITRARY:
      # This is an arbitrary payload.
      payload = header_footer

    if set_parent:
      parent = db.Key.from_path(
          FeedRecord.kind(), FeedRecord.create_key_name(topic))
    else:
      parent = None

    if isinstance(payload, unicode):
      payload = payload.encode('utf-8')

    return cls(
        parent=parent,
        topic=topic,
        topic_hash=sha1_hash(topic),
        payload=db.Blob(payload),
        last_modified=now(),
        content_type=content_type,
        max_failures=max_failures)

  def get_next_subscribers(self, chunk_size=None):
    """Retrieve the next set of subscribers to attempt delivery for this event.

    Args:
      chunk_size: How many subscribers to retrieve at a time while delivering
        the event. Defaults to EVENT_SUBSCRIBER_CHUNK_SIZE.

    Returns:
      Tuple (more_subscribers, subscription_list) where:
        more_subscribers: True if there are more subscribers to deliver to
          after the returned 'subscription_list' has been contacted; this value
          should be passed to update() after the delivery is attempted.
        subscription_list: List of Subscription entities to attempt to contact
          for this event.
    """
    if chunk_size is None:
      chunk_size = EVENT_SUBSCRIBER_CHUNK_SIZE

    if self.delivery_mode == EventToDeliver.NORMAL:
      all_subscribers = Subscription.get_subscribers(
          self.topic, chunk_size + 1, starting_at_callback=self.last_callback)
      if all_subscribers:
        self.last_callback = all_subscribers[-1].callback
      else:
        self.last_callback = ''

      more_subscribers = len(all_subscribers) > chunk_size
      subscription_list = all_subscribers[:chunk_size]
    elif self.delivery_mode == EventToDeliver.RETRY:
      next_chunk = self.failed_callbacks[:chunk_size]
      more_subscribers = len(self.failed_callbacks) > len(next_chunk)

      if self.last_callback:
        # If the final index is present in the next chunk, that means we've
        # wrapped back around to the beginning and will need to do more
        # exponential backoff. This also requires updating the last_callback
        # in the update() method, since we do not know which callbacks from
        # the next chunk will end up failing.
        final_subscription_key = datastore_types.Key.from_path(
            Subscription.__name__,
            Subscription.create_key_name(self.last_callback, self.topic))
        try:
          final_index = next_chunk.index(final_subscription_key)
        except ValueError:
          pass
        else:
          more_subscribers = False
          next_chunk = next_chunk[:final_index]

      subscription_list = [x for x in db.get(next_chunk) if x is not None]
      if subscription_list and not self.last_callback:
        # This must be the first time through the current iteration where we do
        # not yet know a sentinal value in the list that represents the starting
        # point.
        self.last_callback = subscription_list[0].callback

      # If the failed callbacks fail again, they will be added back to the
      # end of the list.
      self.failed_callbacks = self.failed_callbacks[len(next_chunk):]

    return more_subscribers, subscription_list

  def update(self,
             more_callbacks,
             more_failed_callbacks,
             now=datetime.datetime.utcnow,
             max_failures=MAX_DELIVERY_FAILURES,
             retry_period=DELIVERY_RETRY_PERIOD):
    """Updates an event with work progress or deletes it if it's done.

    Reschedules another Task to run to handle this event delivery if needed.

    Args:
      more_callbacks: True if there are more callbacks to deliver, False if
        there are no more subscribers to deliver for this feed.
      more_failed_callbacks: Iterable of Subscription entities for this event
        that failed to deliver.
      max_failures: Maximum failures to allow before giving up.
      retry_period: Initial period for doing exponential (base-2) backoff.
      now: Returns the current time as a UTC datetime.
    """
    self.last_modified = now()

    # Ensure the list of failed callbacks is in sorted order so we keep track
    # of the last callback seen in alphabetical order of callback URL hashes.
    more_failed_callbacks = sorted(more_failed_callbacks,
                                   key=lambda x: x.callback_hash)

    self.failed_callbacks.extend(e.key() for e in more_failed_callbacks)
    if not more_callbacks and not self.failed_callbacks:
      logging.info('EventToDeliver complete: topic = %s, delivery_mode = %s',
                   self.topic, self.delivery_mode)
      self.delete()
      return
    elif not more_callbacks:
      self.last_callback = ''
      self.retry_attempts += 1
      if self.max_failures is not None:
        max_failures = self.max_failures
      if self.retry_attempts > max_failures:
        self.totally_failed = True
      else:
        retry_delay = retry_period * (2 ** (self.retry_attempts-1))
        try:
          self.last_modified += datetime.timedelta(seconds=retry_delay)
        except OverflowError:
          pass

      if self.delivery_mode == EventToDeliver.NORMAL:
        logging.debug('Normal delivery done; %d broken callbacks remain',
                      len(self.failed_callbacks))
        self.delivery_mode = EventToDeliver.RETRY
      else:
        logging.debug('End of attempt %d; topic = %s, subscribers = %d, '
                      'waiting until %s or totally_failed = %s',
                      self.retry_attempts, self.topic,
                      len(self.failed_callbacks), self.last_modified,
                      self.totally_failed)

    def txn():
      self.put()
      if not self.totally_failed:
        self.enqueue()
    db.run_in_transaction(txn)

  def enqueue(self):
    """Enqueues a Task that will execute this EventToDeliver."""
    RETRIES = 3
    if self.delivery_mode == EventToDeliver.RETRY:
      target_queue = EVENT_RETRIES_QUEUE
    elif os.environ.get('HTTP_X_APPENGINE_QUEUENAME') == POLLING_QUEUE:
      target_queue = POLLING_QUEUE
    else:
      target_queue = EVENT_QUEUE
    for i in xrange(RETRIES):
      try:
        taskqueue.Task(
            url='/work/push_events',
            eta=self.last_modified,
            params={'event_key': self.key()}
            ).add(target_queue, transactional=True)
      except (taskqueue.Error, apiproxy_errors.Error):
        logging.exception('Could not insert task to deliver '
                          'events for topic = %s', self.topic)
        if i == (RETRIES - 1):
          raise
      else:
        return


class KnownFeed(db.Model):
  """Represents a feed that we know exists.

  This entity will be overwritten anytime someone subscribes to this feed. The
  benefit is we have a single entity per known feed, allowing us to quickly
  iterate through all of them. This may have issues if the subscription rate
  for a single feed is over one per second.
  """

  topic = db.TextProperty(required=True)
  feed_id = db.TextProperty()
  update_time = db.DateTimeProperty(auto_now=True)

  @classmethod
  def create(cls, topic):
    """Creates a new KnownFeed.

    Args:
      topic: The feed's topic URL.

    Returns:
      The KnownFeed instance that hasn't been added to the Datastore.
    """
    return cls(key_name=get_hash_key_name(topic), topic=topic)

  @classmethod
  def record(cls, topic):
    """Enqueues a task to create a new KnownFeed and initiate feed ID discovery.

    Args:
      topic: The feed's topic URL.
    """
    RETRIES = 3
    target_queue = MAPPINGS_QUEUE
    for i in xrange(RETRIES):
      try:
        taskqueue.Task(
            url='/work/record_feeds',
            params={'topic': topic}
            ).add(target_queue)
      except (taskqueue.Error, apiproxy_errors.Error):
        logging.exception('Could not insert task to do feed ID '
                          'discovery for topic = %s', topic)
        if i == (RETRIES - 1):
          raise
      else:
        return

  @classmethod
  def create_key(cls, topic):
    """Creates a key for a KnownFeed.

    Args:
      topic: The feed's topic URL.

    Returns:
      Key instance for this feed.
    """
    return datastore_types.Key.from_path(cls.kind(), get_hash_key_name(topic))

  @classmethod
  def check_exists(cls, topics):
    """Checks if the supplied topic URLs are known feeds.

    Args:
      topics: Iterable of topic URLs.

    Returns:
      List of topic URLs with KnownFeed entries. If none are known, this list
      will be empty. The returned order is arbitrary.
    """
    result = []
    for known_feed in cls.get([cls.create_key(url) for url in set(topics)]):
      if known_feed is not None:
        result.append(known_feed.topic)
    return result


class KnownFeedStats(db.Model):
  """Represents stats about a feed we know that exists.

  Parent is the KnownFeed entity for a given topic URL.
  """

  subscriber_count = db.IntegerProperty()
  update_time = db.DateTimeProperty(auto_now=True)

  @classmethod
  def create_key(cls, topic_url=None, topic_hash=None):
    """Creates a key for a KnownFeedStats instance.

    Args:
      topic_url: The topic URL to create the key for.
      topic_hash: The hash of the topic URL to create the key for. May only
        be supplied if topic_url is None.

    Returns:
      db.Key of the KnownFeedStats instance.
    """
    if topic_url and topic_hash:
      raise TypeError('Must specify topic_url or topic_hash.')
    if topic_url:
      topic_hash = sha1_hash(topic_url)

    return db.Key.from_path(KnownFeed.kind(), topic_hash,
                            cls.kind(), 'overall')

  @classmethod
  def get_or_create_all(cls, topic_list):
    """Retrieves and/or creates KnownFeedStats entities for the supplied topics.

    Args:
      topic_list: List of topics to retrieve.

    Returns:
      The list of KnownFeedStats corresponding to the input topic list in
      the same order they were supplied.
    """
    key_list = [cls.create_key(t) for t in topic_list]
    found_list = db.get(key_list)
    results = []
    for topic, key, found in zip(topic_list, key_list, found_list):
      if found:
        results.append(found)
      else:
        results.append(cls(key=key, subscriber_count=0))
    return results


class PollingMarker(db.Model):
  """Keeps track of the current position in the bootstrap polling process."""

  last_start = db.DateTimeProperty()
  next_start = db.DateTimeProperty(required=True)

  @classmethod
  def get(cls, now=datetime.datetime.utcnow):
    """Returns the current PollingMarker, creating it if it doesn't exist.

    Args:
      now: Returns the current time as a UTC datetime.
    """
    key_name = 'The Mark'
    the_mark = db.get(datastore_types.Key.from_path(cls.kind(), key_name))
    if the_mark is None:
      next_start = now() - datetime.timedelta(seconds=60)
      the_mark = PollingMarker(key_name=key_name,
                               next_start=next_start,
                               current_key=None)
    return the_mark

  def should_progress(self,
                      period=POLLING_BOOTSTRAP_PERIOD,
                      now=datetime.datetime.utcnow):
    """Returns True if the bootstrap polling should progress.

    May modify this PollingMarker to when the next polling should start.

    Args:
      period: The poll period for bootstrapping.
      now: Returns the current time as a UTC datetime.
    """
    now_time = now()
    if self.next_start < now_time:
      logging.info('Polling starting afresh for start time %s', self.next_start)
      self.last_start = self.next_start
      self.next_start = now_time + datetime.timedelta(seconds=period)
      return True
    else:
      return False


class KnownFeedIdentity(db.Model):
  """Stores a set of known URL aliases for a particular feed."""

  feed_id = db.TextProperty(required=True)
  topics = db.ListProperty(db.Text)
  last_update = db.DateTimeProperty()

  @classmethod
  def create_key(cls, feed_id):
    """Creates a key for a KnownFeedIdentity.

    Args:
      feed_id: The feed's identity. For Atom this is the //feed/id element;
        for RSS it is the //rss/channel/link element. If for whatever reason
        the ID is missing, then the feed URL itself should be used.

    Returns:
      Key instance for this feed identity.
    """
    return datastore_types.Key.from_path(cls.kind(), get_hash_key_name(feed_id))

  @classmethod
  def update(cls, feed_id, topic):
    """Updates a KnownFeedIdentity to have a topic URL mapping.

    Args:
      feed_id: The identity of the feed to update with the mapping.
      topic: The topic URL to add to the feed's list of aliases.

    Returns:
      The KnownFeedIdentity that has been created or updated.
    """
    def txn():
      known_feed = db.get(cls.create_key(feed_id))
      if not known_feed:
        known_feed = cls(feed_id=feed_id, key_name=get_hash_key_name(feed_id))
      if topic not in known_feed.topics:
        known_feed.topics.append(db.Text(topic))
      known_feed.last_update = datetime.datetime.now()
      known_feed.put()
      return known_feed
    try:
      return db.run_in_transaction(txn)
    except (db.BadRequestError, apiproxy_errors.RequestTooLargeError):
      logging.exception(
          'Could not update feed_id=%r; expansion is already too large',
          feed_id)

  @classmethod
  def remove(cls, feed_id, topic):
    """Updates a KnownFeedIdentity to no longer have a topic URL mapping.

    Args:
      feed_id: The identity of the feed to update with the mapping.
      topic: The topic URL to remove from the feed's list of aliases.

    Returns:
      The KnownFeedIdentity that has been updated or None if the mapping
      did not exist previously or has now been deleted because it has no
      active mappings.
    """
    def txn():
      known_feed = db.get(cls.create_key(feed_id))
      if not known_feed:
        return None
      try:
        known_feed.topics.remove(db.Text(topic))
      except ValueError:
        return None

      if not known_feed.topics:
        known_feed.delete()
        return None
      else:
        known_feed.last_update = datetime.datetime.now()
        known_feed.put()
        return known_feed
    return db.run_in_transaction(txn)

  @classmethod
  def derive_additional_topics(cls, topics):
    """Derives topic URL aliases from a set of topics by using feed IDs.

    If a topic URL has a KnownFeed entry but no valid feed_id or
    KnownFeedIdentity record, the input topic will be echoed in the output
    dictionary directly. This properly handles the case where the feed_id has
    not yet been recorded for the feed.

    Args:
      topics: Iterable of topic URLs.

    Returns:
      Dictionary mapping input topic URLs to their full set of aliases,
      including the input topic URL.
    """
    topics = set(topics)
    output_dict = {}
    known_feeds = KnownFeed.get([KnownFeed.create_key(t) for t in topics])

    topics = []
    feed_ids = []
    for feed in known_feeds:
      if feed is None:
        # In case the KnownFeed hasn't been written yet, don't deliver an event;
        # we need the KnownFeed cache to make subscription checking fast.
        continue

      fix_feed_id = feed.feed_id
      if fix_feed_id is not None:
        fix_feed_id = fix_feed_id.strip()

      # No expansion for feeds that have no known topic -> feed_id relation, but
      # record those with KnownFeed as having a mapping from topic -> topic for
      # backwards compatibility with existing production data.
      if fix_feed_id:
        topics.append(feed.topic)
        feed_ids.append(feed.feed_id)
      else:
        output_dict[feed.topic] = set([feed.topic])

    known_feed_ids = cls.get([cls.create_key(f) for f in feed_ids])

    for known_topic, identified in zip(topics, known_feed_ids):
      if identified:
        topic_set = output_dict.get(known_topic)
        if topic_set is None:
          topic_set = set([known_topic])
          output_dict[known_topic] = topic_set
        # TODO(bslatkin): Test this.
        if len(identified.topics) > 25:
          logging.debug('Too many expansion feeds for topic %s: %s',
                        known_topic, identified.topics)
        else:
          topic_set.update(identified.topics)

    return output_dict

################################################################################
# Subscription handlers and workers

def confirm_subscription(mode, topic, callback, verify_token,
                         secret, lease_seconds, record_topic=True):
  """Confirms a subscription request and updates a Subscription instance.

  Args:
    mode: The mode of subscription confirmation ('subscribe' or 'unsubscribe').
    topic: URL of the topic being subscribed to.
    callback: URL of the callback handler to confirm the subscription with.
    verify_token: Opaque token passed to the callback.
    secret: Shared secret used for HMACs.
    lease_seconds: Number of seconds the client would like the subscription
      to last before expiring. If more than max_lease_seconds, will be capped
      to that value. Should be an integer number.
    record_topic: When True, also cause the topic's feed ID to be recorded
      if this is a new subscription.

  Returns:
    True if the subscription was confirmed properly, False if the subscription
    request encountered an error or any other error has hit.
  """
  logging.debug('Attempting to confirm %s for topic = %r, callback = %r, '
                'verify_token = %r, secret = %r, lease_seconds = %s',
                mode, topic, callback, verify_token, secret, lease_seconds)

  parsed_url = list(urlparse.urlparse(utf8encoded(callback)))
  challenge = get_random_challenge()
  real_lease_seconds = min(lease_seconds, MAX_LEASE_SECONDS)
  params = {
    'hub.mode': mode,
    'hub.topic': utf8encoded(topic),
    'hub.challenge': challenge,
    'hub.lease_seconds': real_lease_seconds,
  }
  if verify_token:
    params['hub.verify_token'] = utf8encoded(verify_token)

  if parsed_url[4]:
    # Preserve subscriber-supplied callback parameters.
    parsed_url[4] = '%s&%s' % (parsed_url[4], urllib.urlencode(params))
  else:
    parsed_url[4] = urllib.urlencode(params)

  adjusted_url = urlparse.urlunparse(parsed_url)

  try:
    response = urlfetch.fetch(adjusted_url, method='get',
                              follow_redirects=False,
                              deadline=MAX_FETCH_SECONDS)
  except urlfetch_errors.Error:
    error_traceback = traceback.format_exc()
    logging.debug('Error encountered while confirming subscription '
                  'to %s for callback %s:\n%s',
                  topic, callback, error_traceback)
    return False

  if 200 <= response.status_code < 300 and response.content == challenge:
    if mode == 'subscribe':
      Subscription.insert(callback, topic, verify_token, secret,
                          lease_seconds=real_lease_seconds)
      if record_topic:
        # Enqueue a task to record the feed and do discovery for it's ID.
        KnownFeed.record(topic)
    else:
      Subscription.remove(callback, topic)
    logging.info('Subscription action verified, '
                 'callback = %s, topic = %s: %s', callback, topic, mode)
    return True
  elif mode == 'subscribe' and response.status_code == 404:
    Subscription.archive(callback, topic)
    logging.info('Subscribe request returned 404 for callback = %s, '
                 'topic = %s; subscription archived', callback, topic)
    return True
  else:
    logging.debug('Could not confirm subscription; encountered '
                  'status %d with content: %s', response.status_code,
                  response.content)
    return False


class SubscribeHandler(webapp2.RequestHandler):
  """End-user accessible handler for Subscribe and Unsubscribe events."""

  def get(self):
    self.response.out.write(str(template.render('subscribe_debug.html', {})))

  @dos.limit(param='hub.callback', count=10, period=1)
  def post(self):
    self.response.headers['Content-Type'] = 'text/plain'

    callback = self.request.get('hub.callback', '')
    topic = self.request.get('hub.topic', '')
    verify_type_list = [s.lower() for s in self.request.get_all('hub.verify')]
    verify_token = unicode(self.request.get('hub.verify_token', ''))
    secret = unicode(self.request.get('hub.secret', '')) or None
    lease_seconds = (
       self.request.get('hub.lease_seconds', '') or str(DEFAULT_LEASE_SECONDS))
    mode = self.request.get('hub.mode', '').lower()

    error_message = None
    if not callback or not is_valid_url(callback):
      error_message = ('Invalid parameter: hub.callback; '
                       'must be valid URI with no fragment and '
                       'optional port %s' % ','.join(VALID_PORTS))
    else:
      callback = normalize_iri(callback)

    if not topic or not is_valid_url(topic):
      error_message = ('Invalid parameter: hub.topic; '
                       'must be valid URI with no fragment and '
                       'optional port %s' % ','.join(VALID_PORTS))
    else:
      topic = normalize_iri(topic)

    enabled_types = [vt for vt in verify_type_list if vt in ('async', 'sync')]
    if not enabled_types:
      error_message = 'Invalid values for hub.verify: %s' % (verify_type_list,)
    else:
      verify_type = enabled_types[0]

    if mode not in ('subscribe', 'unsubscribe'):
      error_message = 'Invalid value for hub.mode: %s' % mode

    if lease_seconds:
      try:
        old_lease_seconds = lease_seconds
        lease_seconds = int(old_lease_seconds)
        if not old_lease_seconds == str(lease_seconds):
          raise ValueError
      except ValueError:
        error_message = ('Invalid value for hub.lease_seconds: %s' %
                         old_lease_seconds)

    if error_message:
      logging.debug('Bad request for mode = %s, topic = %s, '
                    'callback = %s, verify_token = %s, lease_seconds = %s: %s',
                    mode, topic, callback, verify_token,
                    lease_seconds, error_message)
      self.response.out.write(error_message)
      return self.response.set_status(400)

    try:
      # Retrieve any existing subscription for this callback.
      sub = Subscription.get_by_key_name(
          Subscription.create_key_name(callback, topic))

      # Deletions for non-existant subscriptions will be ignored.
      if mode == 'unsubscribe' and not sub:
        return self.response.set_status(204)

      # Enqueue a background verification task, or immediately confirm.
      # We prefer synchronous confirmation.
      if verify_type == 'sync':
        if hooks.execute(confirm_subscription,
              mode, topic, callback, verify_token, secret, lease_seconds):
          return self.response.set_status(204)
        else:
          self.response.out.write('Error trying to confirm subscription')
          return self.response.set_status(409)
      else:
        if mode == 'subscribe':
          Subscription.request_insert(callback, topic, verify_token, secret,
                                      lease_seconds=lease_seconds)
        else:
          Subscription.request_remove(callback, topic, verify_token)
        logging.debug('Queued %s request for callback = %s, '
                      'topic = %s, verify_token = "%s", lease_seconds= %s',
                      mode, callback, topic, verify_token, lease_seconds)
        return self.response.set_status(202)

    except (apiproxy_errors.Error, db.Error,
            runtime.DeadlineExceededError, taskqueue.Error), e:
      logging.debug('Could not verify subscription request. %s: %s',
                    e.__class__.__name__, e)
      self.response.headers['Retry-After'] = '120'
      return self.response.set_status(503)


class SubscriptionConfirmHandler(webapp2.RequestHandler):
  """Background worker for asynchronously confirming subscriptions."""

  @work_queue_only
  def post(self):
    sub_key_name = self.request.get('subscription_key_name')
    next_state = self.request.get('next_state')
    verify_token = self.request.get('verify_token')
    secret = self.request.get('secret') or None
    auto_reconfirm = self.request.get('auto_reconfirm', 'False') == 'True'
    sub = Subscription.get_by_key_name(sub_key_name)
    if not sub:
      logging.debug('No subscriptions to confirm '
                    'for subscription_key_name = %s', sub_key_name)
      return

    if next_state == Subscription.STATE_TO_DELETE:
      mode = 'unsubscribe'
    else:
      # NOTE: If next_state wasn't specified, this is probably an old task from
      # the last version of this code. Handle these tasks by assuming they
      # mant subscribe, which will probably cause less damage.
      mode = 'subscribe'

    if not hooks.execute(confirm_subscription,
        mode, sub.topic, sub.callback,
        verify_token, secret, sub.lease_seconds,
        record_topic=False):
      # After repeated re-confirmation failures for a subscription, assume that
      # the callback is dead and archive it. End-user-initiated subscription
      # requests cannot possibly follow this code path, preventing attacks
      # from unsubscribing callbacks without ownership.
      if (not sub.confirm_failed(next_state, verify_token,
                                 auto_reconfirm=auto_reconfirm,
                                 secret=secret) and
          auto_reconfirm and mode == 'subscribe'):
        logging.info('Auto-renewal subscribe request failed the maximum '
                     'number of times for callback = %s, topic = %s; '
                     'subscription archived', sub.callback, sub.topic)
        Subscription.archive(sub.callback, sub.topic)


class SubscriptionReconfirmHandler(webapp2.RequestHandler):
  """Periodic handler causes reconfirmation for almost expired subscriptions."""

  def __init__(self, request, response, now=time.time, start_map=mapreduce.control.start_map):
    """Initializer."""
    webapp2.RequestHandler.__init__(self, request, response)
    self.now = now
    self.start_map = start_map

  @work_queue_only
  def get(self):
    # Use the name, such that only one of these tasks runs per calendar day.
    name = 'reconfirm-%s' % time.strftime('%Y-%m-%d' , time.gmtime(self.now()))
    try:
      taskqueue.Task(
          url='/work/reconfirm_subscriptions',
          name=name
      ).add(POLLING_QUEUE)
    except (taskqueue.TaskAlreadyExistsError, taskqueue.TombstonedTaskError):
      logging.exception('Could not enqueue FIRST reconfirmation task; '
                        'must have already run today.')

  @work_queue_only
  def post(self):
    self.start_map(
        name='Reconfirm expiring subscriptions',
        handler_spec='offline_jobs.SubscriptionReconfirmMapper.run',
        reader_spec='mapreduce.input_readers.DatastoreInputReader',
        mapper_parameters=dict(
            processing_rate=100000,
            entity_kind='main.Subscription',
            threshold_timestamp=int(
                self.now() + SUBSCRIPTION_CHECK_BUFFER_SECONDS)),
        shard_count=SUBSCRIPTION_RECONFIRM_SHARD_COUNT,
        queue_name=POLLING_QUEUE,
        mapreduce_parameters=dict(
          done_callback='/work/cleanup_mapper',
          done_callback_queue=POLLING_QUEUE))


# TODO(bslatkin): Move this to an offline job.
class SubscriptionCleanupHandler(webapp2.RequestHandler):
  """Background worker for cleaning up deleted Subscription instances."""

  @work_queue_only
  def get(self):
    subscriptions = (Subscription.all()
              .filter('subscription_state =', Subscription.STATE_TO_DELETE)
              .fetch(SUBSCRIPTION_CLEANUP_CHUNK_SIZE))
    if subscriptions:
      logging.info('Cleaning up %d subscriptions', len(subscriptions))
      try:
        db.delete(subscriptions)
      except (db.Error, apiproxy_errors.Error, runtime.DeadlineExceededError):
        logging.exception('Could not clean-up Subscription instances')


class CleanupMapperHandler(webapp2.RequestHandler):
  """Cleans up all data from a Mapper job run."""

  @work_queue_only
  def post(self):
    mapreduce_id = self.request.headers.get('mapreduce-id')
    # TODO: Use Mapper Cleanup API once available.
    db.delete(mapreduce.model.MapreduceControl.get_key_by_job_id(mapreduce_id))
    shards = mapreduce.model.ShardState.find_by_mapreduce_id(mapreduce_id)
    db.delete(shards)
    db.delete(mapreduce.model.MapreduceState.get_key_by_job_id(mapreduce_id))

################################################################################
# Publishing handlers

def preprocess_urls(urls):
  """Preprocesses URLs doing any necessary canonicalization.

  Args:
    urls: Set of URLs.

  Returns:
    Iterable of URLs that have been modified.
  """
  return urls


def derive_sources(request_handler, urls):
  """Derives feed sources for a publish event.

  Args:
    request_handler: webapp.RequestHandler instance for the publish event.
    urls: Set of URLs that were published.
  """
  return {}


class PublishHandlerBase(webapp2.RequestHandler):
  """Base-class for publish ping receiving handlers."""

  def receive_publish(self, urls, success_code, param_name):
    """Receives a publishing event for a set of topic URLs.

    Serves 400 errors on invalid input, 503 retries on insertion failures.

    Args:
      urls: Iterable of URLs that have been published.
      success_code: HTTP status code to return on success.
      param_name: Name of the parameter that will be validated.

    Returns:
      The error message, or an empty string if there are no errors.
    """
    urls = hooks.execute(preprocess_urls, urls)
    for url in urls:
      if not is_valid_url(url):
        self.response.set_status(400)
        return '%s invalid: %s' % (param_name, url)

    # Normalize all URLs. This assumes our web framework has already decoded
    # any POST-body encoded URLs that were passed in to the 'urls' parameter.
    urls = set(normalize_iri(u) for u in urls)

    # Only insert FeedToFetch entities for feeds that are known to have
    # subscribers. The rest will be ignored.
    topic_map = KnownFeedIdentity.derive_additional_topics(urls)
    if not topic_map:
      urls = set()
    else:
      # Expand topic URLs by their feed ID to properly handle any aliases
      # this feed may have active subscriptions for.
      urls = set()
      for topic, value in topic_map.iteritems():
        urls.update(value)
      logging.info('Topics with known subscribers: %s', urls)

    source_dict = hooks.execute(derive_sources, self, urls)

    # Record all FeedToFetch requests here. The background Pull worker will
    # double-check if there are any subscribers that need event delivery and
    # will skip any unused feeds.
    try:
      FeedToFetch.insert(urls, source_dict)
    except (taskqueue.Error, apiproxy_errors.Error, db.Error,
            runtime.DeadlineExceededError, fork_join_queue.Error):
      logging.exception('Failed to insert FeedToFetch records')
      self.response.headers['Retry-After'] = '120'
      self.response.set_status(503)
      return 'Transient error; please try again later'
    else:
      self.response.set_status(success_code)
      return ''


class PublishHandler(PublishHandlerBase):
  """End-user accessible handler for the Publish event."""

  def get(self):
    self.response.out.write(str(template.render('publish_debug.html', {})))

  @dos.limit(count=100, period=1)
  def post(self):
    self.response.headers['Content-Type'] = 'text/plain'

    mode = self.request.get('hub.mode')
    if mode.lower() != 'publish':
      self.response.set_status(400)
      self.response.out.write('hub.mode MUST be "publish"')
      return

    urls = set(self.request.get_all('hub.url'))
    if not urls:
      self.response.set_status(400)
      self.response.out.write('MUST supply at least one hub.url parameter')
      return

    logging.debug('Publish event for %d URLs (showing first 25): %s',
                  len(urls), list(urls)[:25])
    error = self.receive_publish(urls, 204, 'hub.url')
    if error:
      self.response.out.write(error)

################################################################################
# Pulling

def find_feed_updates(topic, format, feed_content,
                      filter_feed=feed_diff.filter):
  """Determines the updated entries for a feed and returns their records.

  Args:
    topic: The topic URL of the feed.
    format: The string 'atom', 'rss', or 'arbitrary'.
    feed_content: The content of the feed, which may include unicode characters.
      For arbitrary content, this is just the content itself.
    filter_feed: Used for dependency injection.

  Returns:
    Tuple (header_footer, entry_list, entry_payloads) where:
      header_footer: The header/footer data of the feed.
      entry_list: List of FeedEntryRecord instances, if any, that represent
        the changes that have occurred on the feed. These records do *not*
        include the payload data for the entry.
      entry_payloads: List of strings containing entry payloads (i.e., the XML
        data for the Atom <entry> or <item>).

  Raises:
    xml.sax.SAXException if there is a parse error.
    feed_diff.Error if the feed could not be diffed for any other reason.
  """
  if format == ARBITRARY:
    return (feed_content, [], [])

  header_footer, entries_map = filter_feed(feed_content, format)

  # Find the new entries we've never seen before, and any entries that we
  # knew about that have been updated.
  STEP = MAX_FEED_ENTRY_RECORD_LOOKUPS
  all_keys = entries_map.keys()
  existing_entries = []
  for position in xrange(0, len(all_keys), STEP):
    key_set = all_keys[position:position+STEP]
    existing_entries.extend(FeedEntryRecord.get_entries_for_topic(
        topic, key_set))

  existing_dict = dict((e.id_hash, e.entry_content_hash)
                       for e in existing_entries if e)
  logging.debug('Retrieved %d feed entries, %d of which have been seen before',
                len(entries_map), len(existing_dict))

  entities_to_save = []
  entry_payloads = []
  for entry_id, new_content in entries_map.iteritems():
    new_content_hash = sha1_hash(new_content)
    new_entry_id_hash = sha1_hash(entry_id)
    # Mark the entry as new if the sha1 hash is different.
    try:
      old_content_hash = existing_dict[new_entry_id_hash]
      if old_content_hash == new_content_hash:
        continue
    except KeyError:
      pass

    entry_payloads.append(new_content)
    entities_to_save.append(FeedEntryRecord.create_entry_for_topic(
        topic, entry_id, new_content_hash))

  return header_footer, entities_to_save, entry_payloads


def pull_feed(feed_to_fetch, fetch_url, headers):
  """Pulls a feed.

  Args:
    feed_to_fetch: FeedToFetch instance to pull.
    fetch_url: The URL to fetch. Should be the same as the topic stored on
      the FeedToFetch instance, but may be different due to redirects.
    headers: Dictionary of headers to use for doing the feed fetch.

  Returns:
    Tuple (status_code, response_headers, content) where:
      status_code: The response status code.
      response_headers: Caseless dictionary of response headers.
      content: The body of the response.

  Raises:
    apiproxy_errors.Error if any RPC errors are encountered. urlfetch.Error if
    there are any fetching API errors.
  """
  response = urlfetch.fetch(
      fetch_url,
      headers=headers,
      follow_redirects=False,
      deadline=MAX_FETCH_SECONDS)
  return response.status_code, response.headers, response.content


def pull_feed_async(feed_to_fetch, fetch_url, headers, async_proxy, callback):
  """Pulls a feed asynchronously.

  The callback's prototype is:
    Args:
      status_code: The response status code.
      response_headers: Caseless dictionary of response headers.
      content: The body of the response.
      exception: apiproxy_errors.Error if any RPC errors are encountered.
        urlfetch.Error if there are any fetching API errors. None if there
        were no errors.

  Args:
    feed_to_fetch: FeedToFetch instance to pull.
    fetch_url: The URL to fetch. Should be the same as the topic stored on
      the FeedToFetch instance, but may be different due to redirects.
    headers: Dictionary of headers to use for doing the feed fetch.
    async_proxy: AsyncAPIProxy to use for fetching and waiting.
    callback: Callback function to call after a response has been received.
  """
  def wrapper(response, exception):
    callback(getattr(response, 'status_code', None),
             getattr(response, 'headers', None),
             getattr(response, 'content', None),
             exception)
  urlfetch_async.fetch(fetch_url,
                       headers=headers,
                       follow_redirects=False,
                       async_proxy=async_proxy,
                       callback=wrapper,
                       deadline=MAX_FETCH_SECONDS)


def inform_event(event_to_deliver, alternate_topics):
  """Helper hook informs the Hub of new notifications.

  This can be used to take an action on every notification processed.

  Args:
    event_to_deliver: The new event to deliver, already submitted.
    alternate_topics: A list of alternative Feed topics that this event
      should be delievered for in addition to the 'event_to_deliver's topic.
  """
  pass


def parse_feed(feed_record,
               headers,
               content,
               true_on_bad_feed=True,
               alternate_topics=None):
  """Parses a feed's content, determines changes, enqueues notifications.

  This function will only enqueue new notifications if the feed has changed.

  Args:
    feed_record: The FeedRecord object of the topic that has new content.
    headers: Dictionary of response headers found during feed fetching (may
        be empty).
    content: The feed document possibly containing new entries.
    true_on_bad_feed: When True, return True when the feed's format is
      beyond hope and there's no chance of parsing it correctly. When
      False the error will be propagated up to the caller with a False
      response to this function.
    alternate_topics: A list of alternative Feed topics that this parsed event
      should be delievered for in addition to the main FeedRecord's topic.

  Returns:
    True if successfully parsed the feed content; False on error.
  """
  # The content-type header is extremely unreliable for determining the feed's
  # content-type. Using a regex search for "<rss" could work, but an RE is
  # just another thing to maintain. Instead, try to parse the content twice
  # and use any hints from the content-type as best we can. This has
  # a bias towards Atom content (let's cross our fingers!). We save the format
  # of the last successful parse in the feed_record instance to speed this up
  # for the next time through.
  if 'rss' in (feed_record.format or feed_record.content_type or ''):
    order = (RSS, ATOM, ARBITRARY)
  else:
    order = (ATOM, RSS, ARBITRARY)

  parse_failures = 0
  for format in order:
    # Parse the feed. If this fails we will give up immediately.
    try:
      header_footer, entities_to_save, entry_payloads = find_feed_updates(
          feed_record.topic, format, content)
      break
    except (xml.sax.SAXException, feed_diff.Error), e:
      error_traceback = traceback.format_exc()
      logging.debug(
          'Could not get entries for content of %d bytes in format "%s" '
          'for topic %r:\n%s',
          len(content), format, feed_record.topic, error_traceback)
      parse_failures += 1
    except LookupError, e:
      error_traceback = traceback.format_exc()
      logging.warning('Could not decode encoding of feed document %s\n%s',
                      feed_record.topic, error_traceback)
      # Yes-- returning True here. This feed is beyond all hope because we just
      # don't support this character encoding presently.
      return true_on_bad_feed

  if parse_failures == len(order):
    logging.error('Could not parse feed %r; giving up:\n%s',
                  feed_record.topic, error_traceback)
    # That's right, we return True. This will cause the fetch to be
    # abandoned on parse failures because the feed is beyond hope!
    return true_on_bad_feed

  # If we have more entities than we'd like to handle, only save a subset of
  # them and force this task to retry as if it failed. This will cause two
  # separate EventToDeliver entities to be inserted for the feed pulls, each
  # containing a separate subset of the data.
  if len(entities_to_save) > MAX_NEW_FEED_ENTRY_RECORDS:
    logging.warning('Found more entities than we can process for topic %r; '
                    'splitting', feed_record.topic)
    entities_to_save = entities_to_save[:MAX_NEW_FEED_ENTRY_RECORDS]
    entry_payloads = entry_payloads[:MAX_NEW_FEED_ENTRY_RECORDS]
    parse_successful = False
  else:
    feed_record.update(headers, header_footer, format)
    parse_successful = True

  if format != ARBITRARY and not entities_to_save:
    logging.debug('No new entries found')
    event_to_deliver = None
  else:
    logging.info(
        'Saving %d new/updated entries for content '
        'format=%r, content_type=%r, header_footer_bytes=%d',
        len(entities_to_save), format, feed_record.content_type,
        len(header_footer))
    event_to_deliver = EventToDeliver.create_event_for_topic(
        feed_record.topic, format, feed_record.content_type,
        header_footer, entry_payloads)
    entities_to_save.insert(0, event_to_deliver)

  entities_to_save.insert(0, feed_record)

  # Segment all entities into smaller groups to reduce the chance of memory
  # errors or too large of requests when the entities are put in a single
  # call to the Datastore API.
  all_entities = []
  STEP = MAX_FEED_RECORD_SAVES
  for position in xrange(0, len(entities_to_save), STEP):
    next_entities = entities_to_save[position:position+STEP]
    all_entities.append(next_entities)

  # Doing this put in a transaction ensures that we have written all
  # FeedEntryRecords, updated the FeedRecord, and written the EventToDeliver
  # at the same time. Otherwise, if any of these fails individually we could
  # drop messages on the floor. If this transaction fails, the whole fetch
  # will be redone and find the same entries again (thus it is idempotent).
  def txn():
    while all_entities:
      group = all_entities.pop(0)
      try:
        db.put(group)
      except (db.BadRequestError, apiproxy_errors.RequestTooLargeError):
        logging.exception('Could not insert %d entities for topic %r; '
                          'splitting in half', len(group), feed_record.topic)
        # Insert the first half at the beginning since we need to make sure that
        # the EventToDeliver gets inserted first.
        all_entities.insert(0, group[len(group)/2:])
        all_entities.insert(0, group[:len(group)/2])
        raise
    if event_to_deliver:
      event_to_deliver.enqueue()

  try:
    for i in xrange(PUT_SPLITTING_ATTEMPTS):
      try:
        db.run_in_transaction(txn)
        break
      except (db.BadRequestError, apiproxy_errors.RequestTooLargeError):
        pass
    else:
      logging.critical('Insertion of event to delivery *still* failing due to '
                       'request size; dropping event for %s', feed_record.topic)
      return true_on_bad_feed
  except (db.TransactionFailedError, db.Timeout):
    # Datastore failure will cause a refetch and reparse of the feed as if
    # the fetch attempt failed, instead of relying on the task queue to do
    # this retry for us. This ensures the queue throughputs stay consistent.
    logging.exception('Could not submit transaction for topic %r',
                      feed_record.topic)
    return False

  # Inform any hooks that there will is a new event to deliver that has
  # been recorded and delivery has begun.
  hooks.execute(inform_event, event_to_deliver, alternate_topics)

  return parse_successful


class PullFeedHandler(webapp2.RequestHandler):
  """Background worker for pulling feeds."""

  def _handle_fetches(self, feed_list):
    """Handles a set of FeedToFetch records that need to be fetched."""
    ready_feed_list = []
    scorer_results = FETCH_SCORER.filter([f.topic for f in feed_list])
    for to_fetch, (allow, percent) in zip(feed_list, scorer_results):
      if not allow:
        logging.warning('Scoring prevented fetch of %r '
                        'with failure rate %.2f%%',
                        to_fetch.topic, 100 * percent)
        to_fetch.done()
      elif not Subscription.has_subscribers(to_fetch.topic):
        logging.debug('Ignoring event because there are no subscribers '
                      'for topic %s', to_fetch.topic)
        to_fetch.done()
      else:
        ready_feed_list.append(to_fetch)

    if not ready_feed_list:
      return

    topic_list = [f.topic for f in ready_feed_list]
    feed_record_list = FeedRecord.get_or_create_all(topic_list)
    feed_stats_list = KnownFeedStats.get_or_create_all(topic_list)
    start_time = time.time()
    reporter = dos.Reporter()
    successful_topics = []
    failed_topics = []

    def create_callback(feed_record, feed_stats, work, fetch_url, attempts):
      return lambda *args: callback(
          feed_record, feed_stats, work, fetch_url, attempts, *args)

    def callback(feed_record, feed_stats, work, fetch_url, attempts,
                 status_code, headers, content, exception):
      should_parse = False
      fetch_success = False
      if exception:
        if isinstance(exception, urlfetch.ResponseTooLargeError):
          logging.warning('Feed response too large for topic %r at url %r; '
                          'skipping', work.topic, fetch_url)
          work.done()
        elif isinstance(exception, urlfetch.InvalidURLError):
          logging.warning('Invalid redirection for topic %r to url %r; '
                          'skipping', work.topic, fetch_url)
          work.done()
        elif isinstance(exception, (apiproxy_errors.Error, urlfetch.Error)):
          logging.warning('Failed to fetch topic %r at url %r. %s: %s',
                          work.topic, fetch_url, exception.__class__, exception)
          work.fetch_failed()
        else:
          logging.critical('Unexpected exception fetching topic %r. %s: %s',
                           work.topic, exception.__class__, exception)
          work.fetch_failed()
      else:
        if status_code == 200:
          should_parse = True
        elif status_code in (301, 302, 303, 307) and 'Location' in headers:
          fetch_url = headers['Location']
          logging.debug('Feed publisher for topic %r returned %d '
                        'redirect to %r', work.topic, status_code, fetch_url)
          if attempts >= MAX_REDIRECTS:
            logging.warning('Too many redirects for topic %r', work.topic)
            work.fetch_failed()
          else:
            # Recurse to do the refetch.
            hooks.execute(pull_feed_async,
                work,
                fetch_url,
                feed_record.get_request_headers(feed_stats.subscriber_count),
                async_proxy,
                create_callback(feed_record, feed_stats, work, fetch_url,
                                attempts + 1))
            return
        elif status_code == 304:
          logging.debug('Feed publisher for topic %r returned '
                        '304 response (cache hit)', work.topic)
          work.done()
          fetch_success = True
        else:
          logging.debug('Received bad response for topic = %r, '
                        'status_code = %s, response_headers = %r',
                        work.topic, status_code, headers)
          work.fetch_failed()

      # Fetch is done one way or another.
      end_time = time.time()
      latency = int((end_time - start_time) * 1000)
      if should_parse:
        if parse_feed(feed_record, headers, content):
          fetch_success = True
          work.done()
        else:
          work.fetch_failed()

      if fetch_success:
        successful_topics.append(work.topic)
      else:
        failed_topics.append(work.topic)
      report_fetch(reporter, work.topic, fetch_success, latency)
      # End callback

    # Fire off a fetch for every work item and wait for all callbacks.
    for work, feed_record, feed_stats in zip(
        ready_feed_list, feed_record_list, feed_stats_list):
      hooks.execute(pull_feed_async,
          work,
          work.topic,
          feed_record.get_request_headers(feed_stats.subscriber_count),
          async_proxy,
          create_callback(feed_record, feed_stats, work, work.topic, 1))

    try:
      async_proxy.wait()
    except runtime.DeadlineExceededError:
      logging.error('Could not finish all fetches due to deadline.')
    else:
      # Only update stats if we are not dealing with a deadlined request.
      FETCH_SCORER.report(successful_topics, failed_topics)
      FETCH_SAMPLER.sample(reporter)

  @work_queue_only
  def post(self):
    topic = self.request.get('topic')
    if topic:
      # For compatibility with old tasks and retry tasks.
      work = FeedToFetch.get_by_topic(topic)
      if not work:
        logging.debug('No feeds to fetch for topic = %s', topic)
        return
      self._handle_fetches([work])
    else:
      work_list = FeedToFetch.FORK_JOIN_QUEUE.pop_request(self.request)
      self._handle_fetches(work_list)

################################################################################
# Event delivery

def push_event(sub, headers, payload, async_proxy, callback):
  """Pushes an event to a single subscriber using an asynchronous API call.

  Args:
    sub: The Subscription instance to push the event to.
    headers: Request headers to use when pushing the event.
    payload: The content body the request should have.
    async_proxy: AsyncAPIProxy to use for registering RPCs.
    callback: Python callable to execute on success or failure. This callback
      has the signature func(sub, result, exception) where sub is the
      Subscription instance, result is the urlfetch.Response instance, and
      exception is any exception encountered, if any.
  """
  urlfetch_async.fetch(sub.callback,
                       method='POST',
                       headers=headers,
                       payload=payload,
                       async_proxy=async_proxy,
                       callback=callback,
                       deadline=MAX_FETCH_SECONDS)


class PushEventHandler(webapp2.RequestHandler):
  """Background worker for pushing events to subscribers."""

  @work_queue_only
  def post(self):
    work = EventToDeliver.get(self.request.get('event_key'))
    if not work:
      logging.debug('No events to deliver.')
      return

    # Retrieve the first N + 1 subscribers; note if we have more to contact.
    more_subscribers, subscription_list = work.get_next_subscribers()
    logging.info('%d more subscribers to contact for: '
                 'topic = %s, delivery_mode = %s',
                 len(subscription_list), work.topic, work.delivery_mode)

    # Keep track of failed callbacks. Do this instead of tracking successful
    # callbacks because the asynchronous API calls could be interrupted by a
    # deadline error. If that happens we'll want to mark all outstanding
    # callback urls as still pending (and thus failed).
    all_callbacks = set(subscription_list)
    failed_callbacks = all_callbacks.copy()
    reporter = dos.Reporter()
    start_time = time.time()

    def callback(sub, result, exception):
      end_time = time.time()
      latency = int((end_time - start_time) * 1000)
      if exception or not (200 <= result.status_code <= 299):
        logging.debug('Could not deliver to target url %s: '
                      'Exception = %r, status_code = %s',
                      sub.callback, exception,
                      getattr(result, 'status_code', 'unknown'))
        report_delivery(reporter, sub.callback, False, latency)
      else:
        failed_callbacks.remove(sub)
        report_delivery(reporter, sub.callback, True, latency)

    def create_callback(sub):
      return lambda *args: callback(sub, *args)

    payload_utf8 = utf8encoded(work.payload)
    scores = DELIVERY_SCORER.filter(s.callback for s in all_callbacks)
    for sub, (allowed, percent) in zip(all_callbacks, scores):
      if not allowed:
        logging.warning(
            'Scoring prevented delivery of %s to %s with failure rate %.2f%%',
            work.topic, sub.callback, 100 * percent)
        # Remove it from the list of all callbacks and failured callbacks.
        # When a callback domain is hurting, we do not further penalize it
        # with more failures, but we leave its standing the same. So it's
        # as if this callback was never even seen. At the beginning of
        # the next scoring period this callback will be allowed again.
        all_callbacks.remove(sub)
        failed_callbacks.remove(sub)
        continue

      headers = {
        # In case there was no content type header.
        'Content-Type': work.content_type or 'text/xml',
        # TODO(bslatkin): add a better test for verify_token here.
        'X-Hub-Signature': 'sha1=%s' % sha1_hmac(
            sub.secret or sub.verify_token or '', payload_utf8),
      }
      hooks.execute(push_event,
          sub, headers, payload_utf8, async_proxy, create_callback(sub))

    try:
      async_proxy.wait()
    except runtime.DeadlineExceededError:
      logging.error('Could not finish all callbacks due to deadline. '
                    'Remaining are: %r', [s.callback for s in failed_callbacks])
    else:
      # Only update stats if we're not dealing with a terminating request.
      DELIVERY_SCORER.report(
          [s.callback for s in (all_callbacks - failed_callbacks)],
          [s.callback for s in failed_callbacks])
      DELIVERY_SAMPLER.sample(reporter)

    work.update(more_subscribers, failed_callbacks)

################################################################################

def take_polling_action(topic_list, poll_type):
  """Takes an action on a set of topics to be polled.

  Args:
    topic_list: The iterable of topic URLs to take a polling action on.
    poll_type: The type of polling to do.
  """
  try:
    if poll_type == 'record':
      for topic in topic_list:
        KnownFeed.record(topic)
    else:
      # Force these FeedToFetch records to be written to disk so we ensure
      # that we will eventually polll the feeds.
      FeedToFetch.insert(topic_list, memory_only=False)
  except (taskqueue.Error, apiproxy_errors.Error,
          db.Error, runtime.DeadlineExceededError,
          fork_join_queue.Error):
    logging.exception('Could not take polling action '
                      'of type %r for topics: %s', poll_type, topic_list)


class PollBootstrapHandler(webapp2.RequestHandler):
  """Boostrap handler automatically polls feeds."""

  @work_queue_only
  def get(self):
    poll_type = self.request.get('poll_type', 'bootstrap')
    the_mark = PollingMarker.get()
    if the_mark.should_progress():
      # Naming the task based on the current start time here allows us to
      # enqueue the *next* task in the polling chain before we've enqueued
      # any of the actual FeedToFetch tasks. This is great because it lets us
      # queue up a ton of tasks in parallel (since the task queue is reentrant).
      #
      # Without the task name present, each intermittent failure in the polling
      # chain would cause an *alternate* sequence of tasks to execute. This
      # causes exponential explosion in the number of tasks (think of an
      # NP diagram or the "multiverse" of time/space). Yikes.
      name = 'poll-' + str(int(time.mktime(the_mark.last_start.utctimetuple())))
      try:
        taskqueue.Task(
            url='/work/poll_bootstrap',
            name=name,
            params=dict(sequence=name, poll_type=poll_type)
        ).add(POLLING_QUEUE)
      except (taskqueue.TaskAlreadyExistsError, taskqueue.TombstonedTaskError):
        logging.exception('Could not enqueue FIRST polling task')

      the_mark.put()

  @work_queue_only
  def post(self):
    sequence = self.request.get('sequence')
    current_key = self.request.get('current_key')
    poll_type = self.request.get('poll_type')
    logging.info('Handling polling for sequence = %s, '
                 'current_key = %r, poll_type = %r',
                 sequence, current_key, poll_type)

    query = KnownFeed.all()
    if current_key:
      query.filter('__key__ >', datastore_types.Key(current_key))
    known_feeds = query.fetch(BOOSTRAP_FEED_CHUNK_SIZE)

    if known_feeds:
      current_key = str(known_feeds[-1].key())
      logging.info('Found %s more feeds to poll, ended at %s',
                   len(known_feeds), known_feeds[-1].topic)
      try:
        taskqueue.Task(
            url='/work/poll_bootstrap',
            name='%s-%s' % (sequence, sha1_hash(current_key)),
            params=dict(sequence=sequence,
                        current_key=current_key,
                        poll_type=poll_type)).add(POLLING_QUEUE)
      except (taskqueue.TaskAlreadyExistsError, taskqueue.TombstonedTaskError):
        logging.exception('Continued polling task already present; '
                          'this work has already been done')
        return

      # TODO(bslatkin): Do more intelligent retrying of polling actions.
      hooks.execute(take_polling_action,
                    [k.topic for k in known_feeds],
                    poll_type)

    else:
      logging.info('Polling cycle complete')
      current_key = None

################################################################################
# Feed canonicalization

class RecordFeedHandler(webapp2.RequestHandler):
  """Background worker for categorizing/classifying feed URLs by their ID."""

  def __init__(self, request, response, now=datetime.datetime.now):
    """Initializer.

    Args:
      now: Callable that returns the current time as a datetime.datetime.
    """
    webapp2.RequestHandler.__init__(self, request, response)
    self.now = now

  @work_queue_only
  def post(self):
    topic = self.request.get('topic')
    logging.debug('Recording topic = %s', topic)

    known_feed_key = KnownFeed.create_key(topic)
    known_feed = KnownFeed.get(known_feed_key)
    if known_feed:
      seconds_since_update = self.now() - known_feed.update_time
      if known_feed.feed_id and (seconds_since_update <
          datetime.timedelta(seconds=FEED_IDENTITY_UPDATE_PERIOD)):
        logging.debug('Ignoring feed identity update for topic = %s '
                      'due to update %s ago', topic, seconds_since_update)
        return
    else:
      known_feed = KnownFeed.create(topic)

    try:
      response = urlfetch.fetch(topic)
    except (apiproxy_errors.Error, urlfetch.Error), e:
      logging.warning('Could not fetch topic = %s for feed ID. %s: %s',
                      topic, e.__class__.__name__, e)
      known_feed.put()
      return

    # TODO(bslatkin): Add more intelligent retrying of feed identification.
    if response.status_code != 200:
      logging.warning('Fetching topic = %s for feed ID returned response %s',
                      topic, response.status_code)
      known_feed.put()
      return

    order = (ATOM, RSS)
    parse_failures = 0
    error_traceback = 'Could not determine feed_id'
    feed_id = None
    for feed_type in order:
      try:
        feed_id = feed_identifier.identify(response.content, feed_type)
        if feed_id is not None:
          break
        else:
          parse_failures += 1
      except Exception:
        error_traceback = traceback.format_exc()
        logging.debug(
            'Could not parse feed for content of %d bytes in format "%s":\n%s',
            len(response.content), feed_type, error_traceback)
        parse_failures += 1

    if parse_failures == len(order) or not feed_id:
      logging.warning('Could not record feed ID for topic=%r, feed_id=%r:\n%s',
                      topic, feed_id, error_traceback)
      known_feed.put()
      # Just give up, since we can't parse it. This case also covers when
      # the character encoding for the document is unsupported or the document
      # is of an arbitrary content type.
      return

    logging.info('For topic = %s found new feed ID %r; old feed ID was %r',
                 topic, feed_id, known_feed.feed_id)

    if known_feed.feed_id and known_feed.feed_id != feed_id:
      logging.info('Removing old feed_id relation from '
                   'topic = %r to feed_id = %r', topic, known_feed.feed_id)
      KnownFeedIdentity.remove(known_feed.feed_id, topic)

    KnownFeedIdentity.update(feed_id, topic)
    known_feed.feed_id = feed_id
    known_feed.put()

################################################################################

class HubHandler(webapp2.RequestHandler):
  """Handler to multiplex subscribe and publish events on the same URL."""

  def get(self):
    context = {
      'host': self.request.host,
    }
    self.response.out.write(str(template.render('welcome.html', context)))

  def post(self):
    mode = self.request.get('hub.mode', '').lower()
    if mode == 'publish':
      handler = PublishHandler()
    elif mode in ('subscribe', 'unsubscribe'):
      handler = SubscribeHandler()
    else:
      self.response.set_status(400)
      self.response.out.write(str('hub.mode is invalid'))
      return
    handler.initialize(self.request, self.response)
    handler.post()


class TopicDetailHandler(webapp2.RequestHandler):
  """Handler that serves topic debugging information to end-users."""

  @dos.limit(count=5, period=60)
  def get(self):
    topic_url = normalize_iri(self.request.get('hub.url'))
    feed = FeedRecord.get_by_key_name(FeedRecord.create_key_name(topic_url))
    if not feed:
      self.response.set_status(400)
      context = {
        'topic_url': topic_url,
        'error': 'Could not find any record for topic URL: ' + topic_url,
      }
    else:
      fetch_score = FETCH_SCORER.filter([topic_url])[0]
      context = {
        'topic_url': topic_url,
        'last_successful_fetch': feed.last_updated,
        'last_content_type': feed.content_type,
        'last_etag': feed.etag,
        'last_modified': feed.last_modified,
        'last_header_footer': feed.header_footer,
        'fetch_blocked': not fetch_score[0],
        'fetch_errors': fetch_score[1] * 100,
        'fetch_url_error': FETCH_SAMPLER.get_chain(
            FETCH_URL_SAMPLE_MINUTE,
            FETCH_URL_SAMPLE_30_MINUTE,
            FETCH_URL_SAMPLE_HOUR,
            FETCH_URL_SAMPLE_DAY,
            single_key=topic_url),
        'fetch_url_latency': FETCH_SAMPLER.get_chain(
            FETCH_URL_SAMPLE_MINUTE_LATENCY,
            FETCH_URL_SAMPLE_30_MINUTE_LATENCY,
            FETCH_URL_SAMPLE_HOUR_LATENCY,
            FETCH_URL_SAMPLE_DAY_LATENCY,
            single_key=topic_url),
      }

      if users.is_current_user_admin():
        feed_stats = db.get(KnownFeedStats.create_key(topic_url=topic_url))
        if feed_stats:
          context.update({
            'subscriber_count': feed_stats.subscriber_count,
            'feed_stats_update_time': feed_stats.update_time,
          })

      fetch = FeedToFetch.get_by_topic(topic_url)
      if fetch:
        context.update({
          'next_fetch': fetch.eta,
          'fetch_attempts': fetch.fetching_failures,
          'totally_failed': fetch.totally_failed,
        })
    self.response.out.write(str(template.render('topic_details.html', context)))


class SubscriptionDetailHandler(webapp2.RequestHandler):
  """Handler that serves details about subscriber deliveries to end-users."""

  @dos.limit(count=5, period=60)
  def get(self):
    topic_url = normalize_iri(self.request.get('hub.topic'))
    callback_url = normalize_iri(self.request.get('hub.callback'))
    secret = normalize_iri(self.request.get('hub.secret'))
    subscription = Subscription.get_by_key_name(
        Subscription.create_key_name(callback_url, topic_url))
    callback_domain = dos.get_url_domain(callback_url)

    context = {
      'topic_url': topic_url,
      'callback_url': callback_url,
      'callback_domain': callback_domain,
    }

    if not subscription or (
        not users.is_current_user_admin() and
        subscription.secret and
        subscription.secret != secret):
      context.update({
        'error': 'Could not find any subscription for '
                 'the given (callback, topic, secret) tuple'
      })
    else:
      failed_events = (EventToDeliver.all()
        .filter('failed_callbacks =', subscription.key())
        .fetch(25))
      delivery_score = DELIVERY_SCORER.filter([callback_url])[0]

      context.update({
        'created_time': subscription.created_time,
        'last_modified': subscription.last_modified,
        'lease_seconds': subscription.lease_seconds,
        'expiration_time': subscription.expiration_time,
        'confirm_failures': subscription.confirm_failures,
        'subscription_state': subscription.subscription_state,
        'failed_events': [
          {
            'last_modified': e.last_modified,
            'retry_attempts': e.retry_attempts,
            'totally_failed': e.totally_failed,
            'content_type': e.content_type,
            'payload_trunc': e.payload[:10000],
          }
          for e in failed_events],
        'delivery_blocked': not delivery_score[0],
        'delivery_errors': delivery_score[1] * 100,
        'delivery_url_error': DELIVERY_SAMPLER.get_chain(
            DELIVERY_URL_SAMPLE_MINUTE,
            DELIVERY_URL_SAMPLE_30_MINUTE,
            DELIVERY_URL_SAMPLE_HOUR,
            DELIVERY_URL_SAMPLE_DAY,
            single_key=callback_url),
        'delivery_url_latency': DELIVERY_SAMPLER.get_chain(
            DELIVERY_URL_SAMPLE_MINUTE_LATENCY,
            DELIVERY_URL_SAMPLE_30_MINUTE_LATENCY,
            DELIVERY_URL_SAMPLE_HOUR_LATENCY,
            DELIVERY_URL_SAMPLE_DAY_LATENCY,
            single_key=callback_url),
      })
      # Only show the domain stats when the subscription had a secret.
      if subscription.secret or users.is_current_user_admin():
        context.update({
          'delivery_domain_error': DELIVERY_SAMPLER.get_chain(
              DELIVERY_DOMAIN_SAMPLE_MINUTE,
              DELIVERY_DOMAIN_SAMPLE_30_MINUTE,
              DELIVERY_DOMAIN_SAMPLE_HOUR,
              DELIVERY_DOMAIN_SAMPLE_DAY,
              single_key=callback_url),
          'delivery_domain_latency': DELIVERY_SAMPLER.get_chain(
              DELIVERY_DOMAIN_SAMPLE_MINUTE_LATENCY,
              DELIVERY_DOMAIN_SAMPLE_30_MINUTE_LATENCY,
              DELIVERY_DOMAIN_SAMPLE_HOUR_LATENCY,
              DELIVERY_DOMAIN_SAMPLE_DAY_LATENCY,
              single_key=callback_url),
        })

    self.response.out.write(str(template.render('event_details.html', context)))


class StatsHandler(webapp2.RequestHandler):
  """Handler that serves DoS statistics information."""

  def post(self):
    if self.request.get('action').lower() == 'flush':
      logging.critical('Flushing memcache!')
      memcache.flush_all()
    self.redirect('/stats')

  def get(self):
    context = {
      'fetch_url_error': FETCH_SAMPLER.get_chain(
          FETCH_URL_SAMPLE_MINUTE,
          FETCH_URL_SAMPLE_30_MINUTE,
          FETCH_URL_SAMPLE_HOUR,
          FETCH_URL_SAMPLE_DAY),
      'fetch_url_latency': FETCH_SAMPLER.get_chain(
          FETCH_URL_SAMPLE_MINUTE_LATENCY,
          FETCH_URL_SAMPLE_30_MINUTE_LATENCY,
          FETCH_URL_SAMPLE_HOUR_LATENCY,
          FETCH_URL_SAMPLE_DAY_LATENCY),
      'fetch_domain_error': FETCH_SAMPLER.get_chain(
          FETCH_DOMAIN_SAMPLE_MINUTE,
          FETCH_DOMAIN_SAMPLE_30_MINUTE,
          FETCH_DOMAIN_SAMPLE_HOUR,
          FETCH_DOMAIN_SAMPLE_DAY),
      'fetch_domain_latency': FETCH_SAMPLER.get_chain(
          FETCH_DOMAIN_SAMPLE_MINUTE_LATENCY,
          FETCH_DOMAIN_SAMPLE_30_MINUTE_LATENCY,
          FETCH_DOMAIN_SAMPLE_HOUR_LATENCY,
          FETCH_DOMAIN_SAMPLE_DAY_LATENCY),
      'delivery_url_error': DELIVERY_SAMPLER.get_chain(
          DELIVERY_URL_SAMPLE_MINUTE,
          DELIVERY_URL_SAMPLE_30_MINUTE,
          DELIVERY_URL_SAMPLE_HOUR,
          DELIVERY_URL_SAMPLE_DAY),
      'delivery_url_latency': DELIVERY_SAMPLER.get_chain(
          DELIVERY_URL_SAMPLE_MINUTE_LATENCY,
          DELIVERY_URL_SAMPLE_30_MINUTE_LATENCY,
          DELIVERY_URL_SAMPLE_HOUR_LATENCY,
          DELIVERY_URL_SAMPLE_DAY_LATENCY),
      'delivery_domain_error': DELIVERY_SAMPLER.get_chain(
          DELIVERY_DOMAIN_SAMPLE_MINUTE,
          DELIVERY_DOMAIN_SAMPLE_30_MINUTE,
          DELIVERY_DOMAIN_SAMPLE_HOUR,
          DELIVERY_DOMAIN_SAMPLE_DAY),
      'delivery_domain_latency': DELIVERY_SAMPLER.get_chain(
          DELIVERY_DOMAIN_SAMPLE_MINUTE_LATENCY,
          DELIVERY_DOMAIN_SAMPLE_30_MINUTE_LATENCY,
          DELIVERY_DOMAIN_SAMPLE_HOUR_LATENCY,
          DELIVERY_DOMAIN_SAMPLE_DAY_LATENCY),
    }
    all_configs = []
    all_configs.extend(FETCH_SAMPLER.configs)
    all_configs.extend(DELIVERY_SAMPLER.configs)
    context.update({
      'all_configs': all_configs,
      'show_everything': True,
    })
    self.response.out.write(str(template.render('all_stats.html', context)))

################################################################################
# Hook system

class InvalidHookError(Exception):
  """A module has tried to access a hook for an unknown function."""


class Hook(object):
  """A conditional hook that overrides or modifies Hub behavior.

  Each Hook corresponds to a single Python callable that may be overridden
  by the hook system. Multiple Hooks may inspect or modify the parameters, but
  only a single callable may elect to actually handle the call. The inspect()
  method will be called for each hook in the order the hooks are imported
  by the HookManager. The final set of parameters will be passed to the
  targetted hook's __call__() method. If more than one Hook elects to execute
  a hooked function, a warning logging message be issued and the *first* Hook
  encountered will be executed.
  """

  def inspect(self, args, kwargs):
    """Inspects a hooked function's parameters, possibly modifying them.

    Args:
      args: List of positional arguments for the hook call.
      kwargs: Dictionary of keyword arguments for the hook call.

    Returns:
      True if this Hook should handle the call, False otherwise.
    """
    return False

  def __call__(self, *args, **kwargs):
    """Handles the hook call.

    Args:
      *args, **kwargs: Parameters matching the original function's signature.

    Returns:
      The return value expected by the original function.
    """
    assert False, '__call__ method not defined for %s' % self.__class__


class HookManager(object):
  """Manages registering and loading Hooks from external modules.

  Hook modules will have a copy of this 'main' module's contents in their
  globals dictionary and the Hooks class to be sub-classed. They will also
  have the 'register' method, which the hook module should use to register any
  Hook sub-classes that it defines.

  The 'register' method has the same signature as the _register method of
  this class, but without the leading 'filename' argument; that value is
  curried by the HookManager.
  """

  def __init__(self):
    """Initializer."""
    # Maps hook functions to a list of (filename, Hook) tuples.
    self._mapping = {}

  def load(self, hooks_path='hooks', globals_dict=None):
    """Loads all hooks from a particular directory.

    Args:
      hooks_path: Optional. Relative path to the application directory or
        absolute path to load hook modules from.
      globals_dict: Dictionary of global variables to use when loading the
        hook module. If None, defaults to the contents of this 'main' module.
        Only for use in testing!
    """
    if globals_dict is None:
      globals_dict = globals()

    hook_directory = os.path.join(os.getcwd(), hooks_path)
    if not os.path.exists(hook_directory):
      return
    module_list = os.listdir(hook_directory)
    for module_name in sorted(module_list):
      if not module_name.endswith('.py'):
        continue
      module_path = os.path.join(hook_directory, module_name)
      context_dict = globals_dict.copy()
      context_dict.update({
        'Hook': Hook,
        'register': lambda *a, **k: self._register(module_name, *a, **k)
      })
      logging.debug('Loading hook "%s" from %s', module_name, module_path)
      try:
        exec open(module_path) in context_dict
      except:
        logging.exception('Error loading hook "%s" from %s',
                          module_name, module_path)
        raise

  def declare(self, original):
    """Declares a function as being hookable.

    Args:
      original: Python callable that may be hooked.
    """
    self._mapping[original] = []

  def execute(self, original, *args, **kwargs):
    print('test');
    """Executes a hookable method, possibly invoking a registered Hook.

    Args:
      original: The original hooked callable.
      args: Positional arguments to pass to the callable.
      kwargs: Keyword arguments to pass to the callable.

    Returns:
      Whatever value is returned by the hooked call.
    """
    try:
      hook_list = self._mapping[original]
    except KeyError, e:
      raise InvalidHookError(e)

    modifiable_args = list(args)
    modifiable_kwargs = dict(kwargs)
    matches = []
    for filename, hook in hook_list:
      if hook.inspect(modifiable_args, modifiable_kwargs):
        matches.append((filename, hook))

    filename = __name__
    designated_hook = original
    if len(matches) >= 1:
      filename, designated_hook = matches[0]

    if len(matches) > 1:
      logging.critical(
          'Found multiple matching hooks for %s in files: %s. '
          'Will use the first hook encountered: %s',
          original, [f for (f, hook) in matches], filename)

    return designated_hook(*args, **kwargs)

  def _register(self, filename, original, hook):
    """Registers a Hook to inspect and potentially execute a hooked function.

    Args:
      filename: The name of the hook module this Hook is defined in.
      original: The Python callable of the original hooked function.
      hook: The Hook to register for this hooked function.

    Raises:
      InvalidHookError if the original hook function is not known.
    """
    try:
      self._mapping[original].append((filename, hook))
    except KeyError, e:
      raise InvalidHookError(e)

  def override_for_test(self, original, test):
    """Adds a hook function for testing.

    Args:
      original: The Python callable of the original hooked function.
      test: The callable to use to override the original for this hook function.
    """
    class OverrideHook(Hook):
      def inspect(self, args, kwargs):
        return True
      def __call__(self, *args, **kwargs):
        return test(*args, **kwargs)
    self._register(__name__, original, OverrideHook())

  def reset_for_test(self, original):
    """Clears the configured test hook for a hooked function.

    Args:
      original: The Python callable of the original hooked function.
    """
    self._mapping[original].pop()

################################################################################

HANDLERS = []


def modify_handlers(handlers):
  """Modifies the set of web request handlers.

  Args:
    handlers: List of (path_regex, webapp.RequestHandler) instances that are
      configured for this application.

  Returns:
    Modified list of handlers, with some possibly removed and others added.
  """
  return handlers


def main():
  global HANDLERS
  if not HANDLERS:
    HANDLERS = hooks.execute(modify_handlers, [
      # External interfaces
      (r'/', HubHandler),
      (r'/publish', PublishHandler),
      (r'/subscribe', SubscribeHandler),
      (r'/topic-details', TopicDetailHandler),
      (r'/subscription-details', SubscriptionDetailHandler),
      (r'/stats', StatsHandler),
      # Low-latency workers
      (r'/work/subscriptions', SubscriptionConfirmHandler),
      (r'/work/pull_feeds', PullFeedHandler),
      (r'/work/push_events', PushEventHandler),
      (r'/work/record_feeds', RecordFeedHandler),
      # Periodic workers
      (r'/work/poll_bootstrap', PollBootstrapHandler),
      (r'/work/subscription_cleanup', SubscriptionCleanupHandler),
      (r'/work/reconfirm_subscriptions', SubscriptionReconfirmHandler),
      (r'/work/cleanup_mapper', CleanupMapperHandler),
    ])
  application = webapp2.WSGIApplication(HANDLERS, debug=DEBUG)
  wsgiref.handlers.CGIHandler().run(application)

################################################################################
# Declare and load external hooks.

hooks = HookManager()
hooks.declare(confirm_subscription)
hooks.declare(derive_sources)
hooks.declare(inform_event)
hooks.declare(modify_handlers)
hooks.declare(preprocess_urls)
hooks.declare(pull_feed)
hooks.declare(pull_feed_async)
hooks.declare(push_event)
hooks.declare(take_polling_action)
hooks.load()


if __name__ == '__main__':
  main()
