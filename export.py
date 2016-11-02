import os
import shutil
import xml.etree.ElementTree as etree
import logging
import threading

from unicodedata import normalize
from Queue import Queue
from copy import deepcopy

from logger import get_logger
from conf import conf


logger = get_logger(__name__)


class Exporter:
    REPLACE_CHARS = "\/:*?\"<>|"
    MUSIC_DIR = u".Music"

    instance = None

    def __init__(self, library, volume):
        Exporter.instance = self
        self.library = library
        self.volume = volume
        self.destination = os.path.join("/Volumes", volume)
        self.music_dir = os.path.join(self.destination, Exporter.MUSIC_DIR)
        self._entries = {} # key: filename, value: xml_entry
        self._all_tracks = [] # a list of entries for exporting all tracks (to preserve order of tracks)
        self.message_queue = Queue()  # message queue for real time GUI updating
        self.start_messages = False  # flag that indicates when it is ok to start sending GUI messages
        self.workers = []  # we store worker threads here
        self._cancel = False


    def __del__(self):
        del logger

    def export(self):
        self._check_volume()

        tree = self.library.tree
        collection = tree.getroot().find("COLLECTION")
        locations = []

        for entry in collection:
            if self._cancel:
                return

            location = entry.find("LOCATION")
            modification_info = entry.find("MODIFICATION_INFO")
            src = os.path.join(location.attrib["DIR"].replace("/:", "/"), location.attrib["FILE"])

            if not os.path.exists(src):  # skip non-existing files
                continue
            elif modification_info.attrib["AUTHOR_TYPE"] == "importer" and location.attrib["FILE"].endswith(".wav"):
                # skip recordings
                continue

            # save original locations in order to copy files
            locations.append(src)

            file_name = normalize("NFD", unicode(location.attrib["FILE"]))
            self._entries[file_name] = entry
            self._all_tracks.append(entry)

            location.attrib["DIR"] = "/:" + Exporter.MUSIC_DIR + "/:"
            location.attrib["VOLUME"] = self.volume
            location.attrib["VOLUMEID"] = self.volume

        self._start_copy_thread(locations)

        if conf.remove_orphans:
            self._start_remove_orphan_thread()

        self._start_process_playlists_thread()
        self.start_messages = True

        for worker in self.workers:
            worker.join()

        if not self._cancel:
            logger.info(u"Export finished")

    def get_messages(self):
        if self.start_messages and not any([worker.is_alive() for worker in self.workers]):
            return None

        messages = []

        while not self.message_queue.empty():
            message = self.message_queue.get()
            messages.append(message)

        return messages

    def cancel(self):
        logger.debug("Export cancel")
        self._cancel = True

    def _check_volume(self):
        if not os.path.exists(self.destination):
            raise IOError(u"Volume {0} does not exist.".format(self.volume))

    def _start_remove_orphan_thread(self):
        worker = threading.Thread(target=self._remove_orphan_files)
        self.workers.append(worker)
        worker.start()

        return worker

    def _remove_orphan_files(self):
        logger.debug(u"Removing orphan files")
        file_paths = self._entries.keys()
        orphans = set(os.listdir(self.music_dir)) - set(file_paths)

        for orphan in orphans:
            if self._cancel:
                break

            logger.info(u"Removing {0}".format(orphan))
            self.message_queue.put({"action": "delete", "item": orphan})

            os.remove(os.path.join(self.music_dir, orphan))

    def _start_process_playlists_thread(self):
        worker = threading.Thread(target=self._process_playlists)
        self.workers.append(worker)
        worker.start()

    def _process_playlists(self):
        def recursive_scan(nodes, directory):
            if self._cancel:
                return

            for node in nodes:
                if node.attrib["TYPE"] == "FOLDER":
                    dir_name = self._replace_filename_char(node.attrib["NAME"])
                    new_dir = os.path.join(directory, dir_name)

                    try:
                        os.mkdir(new_dir)
                    except OSError as e:
                        logger.debug(e)

                    recursive_scan(node.find("SUBNODES"), new_dir)

                elif node.attrib["TYPE"] == "PLAYLIST":
                    name = node.attrib["NAME"]
                    if name == "_LOOPS" or name == "_RECORDINGS":
                        continue

                    entries = self._get_playlist_entries(node)
                    self._export_playlist(entries, name, directory)

        # Export all tracks
        self._export_playlist(self._all_tracks, u"All tracks", self.destination)

        # Export playlists
        nodes = self.library.playlists.find("NODE").find("SUBNODES")
        recursive_scan(nodes, self.destination)

    @staticmethod
    def _replace_filename_char(value):
        for c in Exporter.REPLACE_CHARS:
            value = value.replace(c, u'-')
        return value

    def _get_playlist_entries(self, node):
        entries = []

        for playlist_entry in node.find("PLAYLIST"):
            key = playlist_entry.find("PRIMARYKEY")
            file_name = normalize("NFD", unicode(key.attrib["KEY"].split(u'/:')[-1]))

            if file_name in self._entries:
                entries.append(self._entries[file_name])
            else:
                logger.debug(u"Skipping non-existing file {0} ".format(file_name))

        return entries

    def _export_playlist(self, entries, name, directory):
        def create_playlist_entry(parent, entry):
            path = self.library.get_full_path(entry, True, True)

            parent = etree.SubElement(parent, "ENTRY")
            etree.SubElement(parent, "PRIMARYKEY", attrib={"KEY": path, "TYPE": "TRACK"})

        logger.info(u"Exporting playlist {0} to directory {1}".format(name, directory))
        self.message_queue.put({"action": "playlist", "item": name})

        tree = self.library.create_new()
        collection = tree.getroot().find("COLLECTION")
        collection.attrib["ENTRIES"] = str(len(entries))
        playlist = self.library.create_playlist_structure(tree, name, len(entries))

        for entry in entries:
            collection.append(entry)
            create_playlist_entry(playlist, entry)

        name = self._replace_filename_char(name)
        full_path = os.path.join(directory, name + u".nml")
        tree.write(full_path, encoding="utf-8", xml_declaration=True)

    def _start_copy_thread(self, locations):
        worker = threading.Thread(target=self._copy_files, args=(locations, ))
        self.workers.append(worker)
        worker.start()


    def _copy_files(self, locations):

        if not os.path.exists(self.music_dir):
            os.makedirs(self.music_dir)

        for src in locations:
            if self._cancel:
                return

            file_name = os.path.basename(src)
            dest = os.path.join(self.music_dir, file_name)

            try:
                if os.path.exists(src):
                    # skip existing unmodified files
                    if os.path.exists(dest):
                        diff = (os.stat(src).st_mtime - os.stat(dest).st_mtime)
                        if diff < 3602:
                            continue
                        else:
                            logger.debug(u"File exists: {0} // Time difference: {1}".format(src, diff))

                    logger.info(u"Copying {}".format(file_name))
                    self.message_queue.put({"action": "copy", "item": file_name})
                    Exporter._copy(src, dest)
                else:
                    logger.error(src + u" does not exist")

            except IOError as e:
                logger.exception(e)

    @staticmethod
    def _copy(src, dst, buffer_size=10485760):
        '''
        Copies a file to a new location. Much faster performance than Apache Commons due to use of larger buffer
        @param src:    Source File
        @param dst:    Destination File (not file path)
        @param buffer_size:    Buffer size to use during copy
        '''
        #    Check to make sure destination directory exists. If it doesn't create the directory

        try:
            dstParent, dstFileName = os.path.split(dst)
            if(not(os.path.exists(dstParent))):
                os.makedirs(dstParent)

            #    Optimize the buffer for small files
            buffer_size = min(buffer_size, os.path.getsize(src))
            if buffer_size == 0:
                buffer_size = 1024

            if shutil._samefile(src, dst):
                raise shutil.Error(u"`%s` and `%s` are the same file" % (src, dst))
            for fn in [src, dst]:
                try:
                    st = os.stat(fn)
                except OSError:
                    # File most likely does not exist

                    pass
                else:
                    # XXX What about other special files? (sockets, devices...)
                    if shutil.stat.S_ISFIFO(st.st_mode):
                        raise shutil.SpecialFileError(u"`%s` is a named pipe" % fn)
            with open(src, 'rb') as fsrc:
                with open(dst, 'wb') as fdst:
                    shutil.copyfileobj(fsrc, fdst, buffer_size)

            shutil.copystat(src, dst)

        except OSError:
            pass

