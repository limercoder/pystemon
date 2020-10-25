
import logging.handlers
import threading
import time
import random
import os
from collections import deque
import importlib
from pastie.ua import PystemonUA
from pastie.pastie import Pastie

logger = logging.getLogger('pystemon')

class PastieSite(threading.Thread):
    '''
    Instances of these threads are responsible for downloading the list of
    the most recent pastes and add them to the download queue.
    '''

    def __init__(self, name, download_url, archive_url, archive_regex, **kwargs):
        threading.Thread.__init__(self)
        self.kill_received = False
        self.name = name
        self.download_url = download_url
        self.public_url = download_url
        self.archive_url = archive_url
        self.archive_regex = archive_regex
        self.metadata_url = None
        try:
            self.save_dir = kwargs['site_save_dir'] + os.sep + name
            if not os.path.exists(self.save_dir):
                os.makedirs(self.save_dir)
        except KeyError:
            pass
        try:
            self.archive_dir = kwargs['site_archive_dir'] + os.sep + name
            if not os.path.exists(self.archive_dir):
                os.makedirs(self.archive_dir)
        except KeyError:
            pass

        if kwargs['site_public_url'] is not None:
            self.public_url = kwargs['site_public_url']
        if kwargs['site_metadata_url'] is not None:
            self.metadata_url = kwargs['site_metadata_url']

        self.archive_compress = kwargs.get('archive_compress', False)
        self.update_min = kwargs['site_update_min']
        self.update_max = kwargs['site_update_max']
        self.queue = kwargs['site_queue']
        self.user_agent = kwargs.get('site_ua', PystemonUA([]))
        self.patterns = kwargs.get('patterns', [])
        try:
            self.re = kwargs['re']
        except:
            import re
            self.re = re
        self.seen_pasties = deque('', 1000)  # max number of pasties ids in memory
        self.storage = None
        pastie_classname = kwargs['site_pastie_classname']
        if pastie_classname:
            modname = pastie_classname.lower()
            try:
                logger.debug("loading module {0} for pastie site {1}".format(modname, pastie_classname))
                module = importlib.import_module("pastie.sites."+modname)
                logger.debug("module {0} successfully loaded".format(modname))
                self.pastie_class = getattr(module, pastie_classname)
            except Exception as e:
                logger.error("unable to load module {0} for pastie site {1}".format(modname, pastie_classname))
                raise e
        else:
            self.pastie_class = None

    def run(self):
        logger.info('Thread for PastieSite {0} started'.format(self.name))
        while not self.kill_received:
            sleep_time = random.randint(self.update_min, self.update_max)
            try:
                # grabs site from queue
                logger.info(
                    'Downloading list of new pastes from {name}. '
                    'Will check again in {time} seconds'.format(
                        name=self.name, time=sleep_time))
                # get the list of last pasties, but reverse it
                # so we first have the old entries and then the new ones
                last_pasties = self.get_last_pasties()
                if last_pasties:
                    amount = len(last_pasties)
                    while last_pasties:
                        pastie = last_pasties.pop()
                        self.queue.put(pastie)  # add pastie to queue
                        del(pastie)
                    logger.info("Found {amount} new pasties for site {site}. There are now {qsize} pasties to be downloaded.".format(
                        amount=amount,
                        site=self.name,
                        qsize=self.queue.qsize()))
            # catch unknown errors
            except Exception as e:
                msg = 'Thread for {name} crashed unexpectectly, '\
                      'recovering...: {e}'.format(name=self.name, e=e)
                logger.error(msg)
                logger.error(traceback.format_exc())
            finally:
                time.sleep(sleep_time)

    def set_storage(self, storage):
        self.storage = storage

    def save_pastie(self, pastie):
        if self.storage is not None:
            try:
                self.storage.save_pastie(pastie)
            except Exception as e:
                logger.error('Unable to save pastie {0}: {1}'.format(pastie.id, e))

    def get_last_pasties(self):
        # reset the pasties list
        pasties = []
        # populate queue with data
        response = self.user_agent.download_url(self.archive_url)
        if not response:
            logger.warning("Failed to download page {url}".format(url=self.archive_url))
            return False
        htmlPage = response.text
        if not htmlPage:
            logger.warning("No HTML content for page {url}".format(url=self.archive_url))
            return False
        pasties_ids = self.re.findall(self.archive_regex, htmlPage)
        if pasties_ids:
            for pastie_id in pasties_ids:
                # check if the pastie was already downloaded
                # and remember that we've seen it
                if self.seen_pastie_and_remember(pastie_id):
                    # do not append the seen things again in the queue
                    continue
                # pastie was not downloaded yet. Add it to the queue
                if self.pastie_class:
                    pastie = self.pastie_class(self, pastie_id)
                else:
                    pastie = Pastie(self, pastie_id)
                pasties.append(pastie)
            return pasties
        logger.error("No last pasties matches for regular expression site:{site} regex:{regex}. Error in your regex? Dumping htmlPage \n {html}".format(site=self.name, regex=self.archive_regex, html=htmlPage))
        return False

    def seen_pastie(self, pastie_id, **kwargs):
        ''' check if the pastie was already downloaded. '''
        logger.debug('Site[{0}]: Checking if pastie[{1}] was aldready seen'.format(self.name, pastie_id))
        # first look in memory if we have already seen this pastie
        if self.seen_pasties.count(pastie_id):
            logger.debug('Site[{0}]: Pastie[{1}] already in memory'.format(self.name, pastie_id))
            return True
        if self.storage is not None:
            if self.storage.seen_pastie(pastie_id, **kwargs):
                logger.debug('Site[{s}]: Pastie[{id}] found in storage'.format(s=self.name, id=pastie_id))
                return True
        logger.debug('Site[{0}]: Pastie[{1}] is unknown'.format(self.name, pastie_id))
        return False

    def seen_pastie_and_remember(self, pastie_id):
        '''
        Check if the pastie was already downloaded
        and remember that we've seen it
        '''
        if self.seen_pastie(pastie_id,
                            url=self.public_url.format(id=pastie_id),
                            sitename=self.name,
                            filename=self.pastie_id_to_filename(pastie_id)):
            return True
        # We have not yet seen the pastie.
        # Keep in memory that we've seen it using
        # appendleft for performance reasons.
        # (faster later when we iterate over the deque)
        logger.debug('Site[{0}]: Marking pastie[{1}] as seen'.format(self.name, pastie_id))
        return self.seen_pasties.appendleft(pastie_id)

    def pastie_id_to_filename(self, pastie_id):
        filename = pastie_id.replace('/', '_')
        if self.archive_compress:
            filename = filename + ".gz"
        return filename
