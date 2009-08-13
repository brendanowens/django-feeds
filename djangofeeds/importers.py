import sys
import time
import socket
import feedparser
from datetime import datetime, timedelta
from djangofeeds.models import Feed, Post, Enclosure, Category
from yadayada.db import TransactionContext
from django.utils.text import truncate_words, truncate_html_words
from httplib import OK as HTTP_OK
from httplib import MOVED_PERMANENTLY as HTTP_MOVED
from httplib import FOUND as HTTP_FOUND
from httplib import TEMPORARY_REDIRECT as HTTP_TEMPORARY_REDIRECT
from httplib import NOT_FOUND as HTTP_NOT_FOUND
from httplib import NOT_MODIFIED as HTTP_NOT_MODIFIED
from django.utils.translation import ugettext_lazy as _
from djangofeeds import logger as default_logger
from django.conf import settings
from djangofeeds.models import FEED_TIMEDOUT_ERROR, FEED_TIMEDOUT_ERROR_TEXT
from djangofeeds.models import FEED_NOT_FOUND_ERROR, FEED_NOT_FOUND_ERROR_TEXT
from djangofeeds.models import FEED_GENERIC_ERROR, FEED_GENERIC_ERROR_TEXT


ACCEPTED_STATUSES = [HTTP_FOUND, HTTP_MOVED, HTTP_OK, HTTP_TEMPORARY_REDIRECT]
DEFAULT_POST_LIMIT = 20
DEFAULT_NUM_POSTS = -1
DEFAULT_CACHE_MIN = 30
DEFAULT_SUMMARY_MAX_WORDS = 25
DEFAULT_FEED_TIMEOUT = 10
DEFAULT_MIN_REFRESH_INTERVAL = timedelta(seconds=60 * 20)

"""
.. data:: STORE_ENCLOSURES

Keep post enclosures.
Default: False
Taken from: ``settings.DJANGOFEEDS_STORE_ENCLOSURES``.
"""

STORE_ENCLOSURES = getattr(settings, "DJANGOFEEDS_STORE_ENCLOSURES", False)

"""
.. data:: STORE_CATEGORIES

Keep feed/post categories
Default: False
Taken from: ``settings.DJANGOFEEDS_STORE_CATEGORIES``.

"""
STORE_CATEGORIES = getattr(settings, "DJANGOFEEDS_STORE_CATEGORIES", False)

"""
.. data:: MIN_REFRESH_INTERVAL

Feed should not be refreshed if it was last refreshed within this time.
(in seconds)
Default: 20 minutes
Taken from: ``settings.DJANGOFEEDS_MIN_REFRESH_INTERVAL``.

"""
MIN_REFRESH_INTERVAL = getattr(settings, "DJANGOFEEDS_MIN_REFRESH_INTERVAL",
                               DEFAULT_MIN_REFRESH_INTERVAL)

"""
.. data:: FEED_TIMEOUT

Timeout in seconds for the feed to refresh.
Default: 10 seconds
Taken from: ``settings.DJANGOFEEDS_FEED_TIMEOUT``.
"""
FEED_TIMEOUT = getattr(settings, "DJANGOFEEDS_FEED_TIMEOUT",
                       DEFAULT_FEED_TIMEOUT)

# Make sure MIN_REFRESH_INTERVAL is a timedelta object.
if isinstance(MIN_REFRESH_INTERVAL, int):
    MIN_REFRESH_INTERVAL = timedelta(seconds=MIN_REFRESH_INTERVAL)


class TimeoutError(Exception):
    """The operation timed-out."""


class FeedCriticalError(Exception):
    """An unrecoverable error happened that the user must deal with."""
    status = None

    def __init__(self, msg, status=None):
        if status:
            self.status = status
        super(FeedCriticalError, self).__init__(msg, self.status)


class FeedNotFoundError(FeedCriticalError):
    """The feed URL provieded does not exist."""
    status = HTTP_NOT_FOUND


def summarize(text, max_length=DEFAULT_SUMMARY_MAX_WORDS):
    """Truncate words by ``max_length``."""
    return truncate_words(text, max_length)


def summarize_html(text, max_length=DEFAULT_SUMMARY_MAX_WORDS):
    """Truncate HTML by ``max_length``."""
    return truncate_html_words(text, max_length)


def entries_by_date(entries, limit=-1):
    """Sort the feed entries by date

    :param entries: Entries given from :mod:`feedparser``.
    :param limit: Limit number of posts.

    """

    def date_entry_tuple(entry):
        """Find the most current date entry tuple."""
        if "date_parsed" in entry:
            return (entry["date_parsed"], entry)
        if "updated_parsed" in entry:
            return (entry["updated_parsed"], entry)
        if "published_parsed" in entry:
            return (entry["published_parsed"], entry)
        return (time.localtime(), entry)

    sorted_entries = [date_entry_tuple(entry)
                            for entry in entries]
    sorted_entries.sort()
    sorted_entries.reverse()
    return [entry for (date, entry) in sorted_entries[:limit]]


def _find_post_summary(feed_obj, entry):
    """Find the correct summary field for a post."""
    try:
        content = entry["content"][0]["value"]
    except (IndexError, KeyError):
        content = ""
    return summarize_html(entry.get("summary", content))


def _gen_parsed_date_to_datetime(field_name):
    """Given a post field, convert its :mod:`feedparser` date tuple to
    :class:`datetime.datetime` objects.

    :param field_name: The post field to use.
    """

    def _parsed_date_to_datetime(feed_obj, entry):
        """generated below"""
        if field_name in entry:
            try:
                time_ = time.mktime(entry[field_name])
                date = datetime.fromtimestamp(time_)
            except TypeError:
                date = datetime.now()
            return date
        return datetime.now()
    _parsed_date_to_datetime.__doc__ = \
            """Convert %s to :class:`datetime.datetime` object""" % field_name
    return _parsed_date_to_datetime


def truncate_by_field(field, value):
    """Truncate string value by model fields ``max_length`` attribute.

    :param field: A Django model field instance.
    :param value: The value to truncate.

    """
    if isinstance(value, basestring) and \
            hasattr(field, "max_length") and value > field.max_length:
                return value[:field.max_length]
    return value


def truncate_field_data(model, data):
    """Truncate all data fields for model by its ``max_length`` field
    attributes.

    :param model: Kind of data (A Django Model instance).
    :param data: The data to truncate.

    """
    fields = dict([(field.name, field) for field in model._meta.fields])
    return dict([(name, truncate_by_field(fields[name], value))
                    for name, value in data.items()])


class FeedImporter(object):
    """Import/Update feeds.

    :keyword post_limit: See :attr`post_limit`.
    :keyword update_on_import: See :attr:`update_on_import`.
    :keyword logger: See :attr:`logger`.
    :keyword include_categories: See :attr:`include_categories`.
    :keyword include_enclosures: See :attr:`include_enclosures`.
    :keyword timeout: See :attr:`timeout`.

    .. attribute:: post_limit

        Default number of posts limit.

    .. attribute:: update_on_import

        By default, fetch new posts when a feed is imported

    .. attribute:: logger

       The :class:`logging.Logger` instance used for logging messages.

    .. attribute:: include_categories

        By default, include feed/post categories.

    .. attribute:: include_enclosures

        By default, include post enclosures.

    .. attribute:: timeout

        Default feed timeout.

    .. attribute:: parser

        The feed parser used. (Default: :mod:`feedparser`.)

    """
    parser = feedparser
    post_limit = DEFAULT_POST_LIMIT
    include_categories = STORE_CATEGORIES
    include_enclosures = STORE_ENCLOSURES
    update_on_import = True
    post_field_handlers = {
        "content": _find_post_summary,
        "date_published": _gen_parsed_date_to_datetime("published_parsed"),
        "date_updated": _gen_parsed_date_to_datetime("updated_parsed"),
        "link": lambda feed_obj, entry: entry.get("link") or feed_obj.feed_url,
        "feed": lambda feed_obj, entry: feed_obj,
        "guid": lambda feed_obj, entry: entry.get("guid", "").strip(),
        "title": lambda feed_obj, entry: entry.get("title",
                                                    "(no title)").strip(),
        "author": lambda feed_obj, entry: entry.get("author", "").strip(),
    }

    def __init__(self, **kwargs):
        self.post_limit = kwargs.get("post_limit", self.post_limit)
        self.update_on_import = kwargs.get("update_on_import",
                                            self.update_on_import)
        self.logger = kwargs.get("logger", default_logger)
        self.include_categories = kwargs.get("include_categories",
                                        self.include_categories)
        self.include_enclosures = kwargs.get("include_enclosures",
                                        self.include_enclosures)
        self.timeout = kwargs.get("timeout", FEED_TIMEOUT)

    def parse_feed(self, feed_url, etag=None, modified=None, timeout=None):
        """Parse feed using the current feed parser.

        :param feed_url: URL to the feed to parse.

        :keyword etag: E-tag recevied from last parse (if any).
        :keyword modified: ``Last-Modified`` HTTP header received from last
            parse (if any).
        :keyword timeout: Parser timeout in seconds.

        """
        prev_timeout = socket.getdefaulttimeout()
        timeout = timeout or self.timeout

        socket.setdefaulttimeout(timeout)
        try:
            feed = self.parser.parse(feed_url,
                                     etag=etag,
                                     modified=modified)
        finally:
            socket.setdefaulttimeout(prev_timeout)

        return feed

    def import_feed(self, feed_url, force=None):
        """Import feed.

        If feed is not seen before it will be created, otherwise
        just updated.

        :param feed_url: URL to the feed to import.
        :keyword force: Force import of feed even if it's been updated
            too recently.

        """
        logger = self.logger
        feed_url = feed_url.strip()
        feed = None
        try:
            feed_obj = Feed.objects.get(feed_url=feed_url)
        except Feed.DoesNotExist:
            last_error = u""
            logger.debug("Starting import of %s." % feed_url)
            try:
                feed = self.parse_feed(feed_url)
            except socket.timeout:
                error = FEED_TIMEDOUT_ERROR
            except Exception:
                feed = {"status": 500}

            status = feed.get("status", HTTP_NOT_FOUND)
            if status == HTTP_NOT_FOUND:
                raise FeedNotFoundError(unicode(FEED_NOT_FOUND_ERROR_TEXT))
            if status not in ACCEPTED_STATUSES:
                raise FeedCriticalError(unicode(FEED_GENERIC_ERROR_TEXT),
                                        status=status)

            logger.debug("%s parsed" % feed_url)
            # Feed can be local/fetched with a HTTP client.
            status = feed.get("status\n", HTTP_OK)

            if status == HTTP_FOUND or status == HTTP_MOVED:
                if feed_url != feed.href:
                    return self.import_feed(feed.href, force)

            feed_name = feed.channel.get("title", "(no title)").strip()
            feed_data = truncate_field_data(Feed, {
                            "sort": 0,
                            "name": feed_name,
                            "description": feed.channel.get("description", ""),
            })
            feed_data["last_error"] = last_error
            feed_obj, created = Feed.objects.get_or_create(feed_url=feed_url,
                                                           **feed_data)
            if not created:
                feed_obj.save()
            logger.debug("%s Feed object created" % feed_url)

        if self.include_categories:
            feed_obj.categories.add(*self.get_categories(feed.channel))
            logger.debug("%s categories created" % feed_url)
        if self.update_on_import:
            logger.info("%s Updating...." % feed_url)
            feed_obj = self.update_feed(feed_obj, feed=feed, force=force)
            logger.debug("%s Update finished!" % feed_url)
        return feed_obj

    def get_categories(self, obj):
        """Get and save categories."""
        if hasattr(obj, "categories"):
            return [self.create_category(*cat)
                        for cat in obj.categories]
        return []

    def create_category(self, domain, name):
        """Create new category.

        :param domain: The category domain.
        :param name: The name of the category.

        """
        domain = domain.strip()
        name = name.strip()
        fields = {"name": name, "domain": domain}
        cat, created = Category.objects.get_or_create(**fields)
        return cat

    def update_feed(self, feed_obj, feed=None, force=False):
        """Update (refresh) feed.

        The feed must already exist in the system, if not you have
        to import it using :meth:`import_feed`.

        :param feed_obj: URL of the feed to refresh.
        :keyword feed: If feed has already been parsed you can pass the
            structure returned by the parser so it doesn't have to be parsed
            twice.
        :keyword force: Force refresh of the feed even if it has been
            recently refreshed already.

        """
        logger = self.logger

        if feed_obj.date_last_refresh and datetime.now() < \
                feed_obj.date_last_refresh + MIN_REFRESH_INTERVAL:
            logger.info("Feed %s don't need to be refreshed" % \
                                            feed_obj.feed_url)
            return feed_obj

        limit = self.post_limit
        if not feed:
            self.logger.debug("uf: %s Feed was not provided, fetch..." % (
                feed_obj.feed_url))
            last_modified = None
            if feed_obj.http_last_modified:
                self.logger.debug("uf: %s Feed was last modified %s" % (
                        feed_obj.feed_url, feed_obj.http_last_modified))
                last_modified = feed_obj.http_last_modified.timetuple()
            self.logger.debug("uf: Parsing feed %s" % feed_obj.feed_url)
            try:
                feed = self.parse_feed(feed_obj.feed_url,
                                       etag=feed_obj.http_etag,
                                       modified=last_modified)
            except socket.timeout:
                feed_obj.last_error = FEED_TIMEDOUT_ERROR
                feed_obj.save()
                return feed_obj
            except Exception, e:
                feed_obj.last_error = FEED_GENERIC_ERROR
                feed_obj.save()
                return feed_obj

        # Feed can be local/ not fetched with HTTP client.
        status = feed.get("status", HTTP_OK)
        if status == HTTP_NOT_MODIFIED:
            return feed_obj

        self.logger.debug("uf: %s Feed HTTP status is %d" %
                (feed_obj.feed_url, status))

        if status == HTTP_NOT_FOUND:
            feed_obj.last_error = FEED_NOT_FOUND_ERROR
            feed_obj.save()
            return feed_obj

        if status not in ACCEPTED_STATUSES:
            feed_obj.last_error = FEED_GENERIC_ERROR
            feed_obj.save()
            return feed_obj

        self.logger.debug("uf: %s Importing entries..." %
                (feed_obj.feed_url))
        entries = [self.import_entry(entry, feed_obj)
                    for entry in entries_by_date(feed.entries, limit)]
        feed_obj.date_last_refresh = datetime.now()
        feed_obj.http_etag = feed.get("etag", "")
        if hasattr(feed, "modified"):
            feed_obj.http_last_modified = datetime.fromtimestamp(
                                            time.mktime(feed.modified))
        self.logger.debug("uf: %s Saving feed object..." %
                (feed_obj.feed_url))
        feed_obj.last_error = u""
        feed_obj.save()
        return feed_obj

    def create_enclosure(self, **kwargs):
        """Create new enclosure."""
        kwargs["length"] = kwargs.get("length", 0) or 0
        enclosure, created = Enclosure.objects.get_or_create(**kwargs)
        return enclosure

    def get_enclosures(self, entry):
        """Get and create enclosures for feed."""
        if not hasattr(entry, 'enclosures'):
            return []
            return [self.create_enclosure(url=enclosure.href,
                                        length=enclosure.length,
                                        type=enclosure.type)
                        for enclosure in entry.enclosures
                        if enclosure and hasattr(enclosure, "length")]

    def post_fields_parsed(self, entry, feed_obj):
        """Parse post fields."""
        fields = [(key, handler(feed_obj, entry))
                        for key, handler in self.post_field_handlers.items()]
        return dict(fields)

    def import_entry(self, entry, feed_obj):
        """Import feed post entry."""
        self.logger.debug("ie: %s Importing entry..." % (feed_obj.feed_url))
        self.logger.debug("ie: %s parsing field data..." %
                (feed_obj.feed_url))
        fields = self.post_fields_parsed(entry, feed_obj)
        self.logger.debug("ie: %s Truncating field data..." %
                (feed_obj.feed_url))
        fields = truncate_field_data(Post, fields)

        if fields["guid"]:
            # Unique on GUID, feed
            self.logger.debug("ie: %s Is unique on GUID, storing post." % (
                feed_obj.feed_url))
            post, created = Post.objects.get_or_create(
                                            guid=fields["guid"],
                                            feed=feed_obj,
                                            defaults=fields)
        else:
            # Unique on title, feed, date_published
            self.logger.debug("ie: %s No GUID, storing post." % (
                feed_obj.feed_url))
            try:
                lookup_fields = {"title": fields["title"],
                                 "feed": feed_obj,
                                 "date_published": fields["date_published"]}
                post, created = Post.objects.get_or_create(defaults=fields,
                                                           **lookup_fields)
            except Post.MultipleObjectsReturned:
                self.logger.debug("ie: %s Possible duplicate found." % (
                    feed_obj.feed_url))
                dupe = self._find_duplicate_post(lookup_fields, fields)
                if dupe:
                    self.logger.debug("ie: %s Duplicate found: %d" % (
                        feed_obj.feed_url, dupe.pk))
                    post = dupe
                    created = False
                else:
                    self.logger.debug(
                        "ie: %s No duplicates. Creating new post." % (
                            feed_obj.feed_url))
                    post = Post.objects.create(**fields)
                    created = True

        if not created:
            # Update post with new values (if any)
            self.logger.debug("ie: %s Feed not new, update values..." % (
                feed_obj.feed_url))
            post.save()

        if self.include_enclosures:
            self.logger.debug("ie: %s Saving enclosures..." % (
                feed_obj.feed_url))
            enclosures = self.get_enclosures(entry) or []
            post.enclosures.add(*enclosures)
        if self.include_categories:
            self.logger.debug("ie: %s Saving categories..." % (
                feed_obj.feed_url))
            categories = self.get_categories(entry) or []
            post.categories.add(*categories)

        self.logger.debug("ie: %s Post successfully imported..." % (
            feed_obj.feed_url))

        return post

    def _find_duplicate_post(self, lookup_fields, fields):
        # If any of these fields matches, it's a dupe.
        # Compare in order, because you want to compare short fields
        # before having to match the content.
        cmp_fields = ("author", "link", "content")

        for possible in Post.objects.filter(**lookup_fields).iterator():
            for field in cmp_fields:
                orig_attr = getattr(possible_dupe, field, None)
                this_attr = fields.get(field)
                if orig_attr == this_attr:
                    return possible


def print_feed_summary(feed_obj):
    """Dump a summary of the feed (how many posts etc.)."""
    posts = feed_obj.get_posts()
    enclosures_count = sum([post.enclosures.count() for post in posts])
    categories_count = sum([post.categories.count() for post in posts]) \
                        + feed_obj.categories.count()
    sys.stderr.write("*** Total %d posts, %d categories, %d enclosures\n" % \
            (len(posts), categories_count, enclosures_count))


def refresh_all(verbose=True):
    """ Refresh all feeds in the system. """
    importer = FeedImporter()
    for feed_obj in Feed.objects.all():
        sys.stderr.write(">>> Refreshing feed %s...\n" % \
                (feed_obj.name))
        feed_obj = importer.update_feed(feed_obj)

        if verbose:
            print_feed_summary(feed_obj)
