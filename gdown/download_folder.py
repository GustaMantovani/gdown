import collections
import itertools
import json
import os
import os.path as osp
import queue
import re
import sys
import threading
import warnings
from typing import List
from typing import Union

import bs4

from .download import _get_session
from .download import download
from .exceptions import FolderContentsMaximumLimitError
from .parse_url import is_google_drive_url

MAX_NUMBER_FILES = 50


class _GoogleDriveFile(object):
    TYPE_FOLDER = "application/vnd.google-apps.folder"

    def __init__(self, id, name, type, children=None):
        self.id = id
        self.name = name
        self.type = type
        self.children = children if children is not None else []

    def is_folder(self):
        return self.type == self.TYPE_FOLDER


def _parse_google_drive_file(url, content):
    """Extracts information about the current page file and its children."""

    folder_soup = bs4.BeautifulSoup(content, features="html.parser")

    # finds the script tag with window['_DRIVE_ivd']
    encoded_data = None
    for script in folder_soup.select("script"):
        inner_html = script.decode_contents()

        if "_DRIVE_ivd" in inner_html:
            # first js string is _DRIVE_ivd, the second one is the encoded arr
            regex_iter = re.compile(r"'((?:[^'\\]|\\.)*)'").finditer(inner_html)
            # get the second elem in the iter
            try:
                encoded_data = next(itertools.islice(regex_iter, 1, None)).group(1)
            except StopIteration:
                raise RuntimeError("Couldn't find the folder encoded JS string")
            break

    if encoded_data is None:
        raise RuntimeError(
            "Cannot retrieve the folder information from the link. "
            "You may need to change the permission to "
            "'Anyone with the link', or have had many accesses. "
            "Check FAQ in https://github.com/wkentaro/gdown?tab=readme-ov-file#faq.",
        )

    # decodes the array and evaluates it as a python array
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        decoded = encoded_data.encode("utf-8").decode("unicode_escape")
    folder_arr = json.loads(decoded)

    folder_contents = [] if folder_arr[0] is None else folder_arr[0]

    sep = " - "  # unicode dash
    splitted = folder_soup.title.contents[0].split(sep)
    if len(splitted) >= 2:
        name = sep.join(splitted[:-1])
    else:
        raise RuntimeError(
            "file/folder name cannot be extracted from: {}".format(
                folder_soup.title.contents[0]
            )
        )

    gdrive_file = _GoogleDriveFile(
        id=url.split("/")[-1],
        name=name,
        type=_GoogleDriveFile.TYPE_FOLDER,
    )

    id_name_type_iter = [
        (e[0], e[2].encode("raw_unicode_escape").decode("utf-8"), e[3])
        for e in folder_contents
    ]

    return gdrive_file, id_name_type_iter


def _download_and_parse_google_drive_link(
    sess,
    url,
    quiet=False,
    remaining_ok=False,
    verify=True,
):
    """Get folder structure of Google Drive folder URL."""

    return_code = True

    for _ in range(2):
        if is_google_drive_url(url):
            # canonicalize the language into English
            if "?" in url:
                url += "&hl=en"
            else:
                url += "?hl=en"

        res = sess.get(url, verify=verify)
        if res.status_code != 200:
            return False, None

        if is_google_drive_url(url):
            break

        if not is_google_drive_url(res.url):
            break

        # need to try with canonicalized url if the original url redirects to gdrive
        url = res.url

    gdrive_file, id_name_type_iter = _parse_google_drive_file(
        url=url,
        content=res.text,
    )

    for child_id, child_name, child_type in id_name_type_iter:
        if child_type != _GoogleDriveFile.TYPE_FOLDER:
            if not quiet:
                print(
                    "Processing file",
                    child_id,
                    child_name,
                )
            gdrive_file.children.append(
                _GoogleDriveFile(
                    id=child_id,
                    name=child_name,
                    type=child_type,
                )
            )
            if not return_code:
                return return_code, None
            continue

        if not quiet:
            print(
                "Retrieving folder",
                child_id,
                child_name,
            )
        return_code, child = _download_and_parse_google_drive_link(
            sess=sess,
            url="https://drive.google.com/drive/folders/" + child_id,
            quiet=quiet,
            remaining_ok=remaining_ok,
        )
        if not return_code:
            return return_code, None
        gdrive_file.children.append(child)
    has_at_least_max_files = len(gdrive_file.children) == MAX_NUMBER_FILES
    if not remaining_ok and has_at_least_max_files:
        message = " ".join(
            [
                "The gdrive folder with url: {url}".format(url=url),
                "has more than {max} files,".format(max=MAX_NUMBER_FILES),
                "gdrive can't download more than this limit.",
            ]
        )
        raise FolderContentsMaximumLimitError(message)
    return return_code, gdrive_file


def _get_directory_structure(gdrive_file, previous_path):
    """Converts a Google Drive folder structure into a local directory list."""

    directory_structure = []
    for file in gdrive_file.children:
        file.name = file.name.replace(osp.sep, "_")
        if file.is_folder():
            directory_structure.append((None, osp.join(previous_path, file.name)))
            for i in _get_directory_structure(file, osp.join(previous_path, file.name)):
                directory_structure.append(i)
        elif not file.children:
            directory_structure.append((file.id, osp.join(previous_path, file.name)))
    return directory_structure


GoogleDriveFileToDownload = collections.namedtuple(
    "GoogleDriveFileToDownload", ("id", "path", "local_path")
)


def _download_worker(
    output=None,
    quiet=False,
    proxy=None,
    speed=None,
    use_cookies=True,
    verify=True,
    user_agent=None,
    skip_download: bool = False,
    resume=False,
    input_file_queue: queue.Queue = queue.Queue(),
):
    while input_file_queue.qsize() > 0:
        id, path = input_file_queue.get()
        input_file_queue.task_done()
        try:
            if id is None:
                continue
            if id is None and path is None:
                break
            local_path = osp.join(output, path)
            if not skip_download:
                if not osp.exists(osp.dirname(local_path)):
                    os.makedirs(osp.dirname(local_path))
                if resume and osp.exists(local_path):
                    print(f"File {local_path} already exists, skipping download.")
                    continue
                saved_file_name = download(
                    url="https://drive.google.com/uc?id={id}".format(id=id),
                    output=local_path,
                    quiet=quiet,
                    proxy=proxy,
                    speed=speed,
                    use_cookies=use_cookies,
                    verify=verify,
                    user_agent=user_agent,
                    resume=resume,
                )

                if saved_file_name is not None:
                    print(f"Downloaded file {id} to {saved_file_name}", file=sys.stderr)
                else:
                    print(f"Failed to download file {id}", file=sys.stderr)
                    continue
        except Exception as e:
            print(f"Error downloading file {id}: {e}", file=sys.stderr)


def _create_download_workers(
    workers=1,
    output=None,
    quiet=False,
    proxy=None,
    speed=None,
    use_cookies=True,
    verify=True,
    user_agent=None,
    skip_download: bool = False,
    resume=False,
    input_file_queue: queue.Queue = queue.Queue(),
) -> List[threading.Thread]:
    workers_list = []
    for _ in range(workers):
        workers_list.append(
            threading.Thread(
                target=_download_worker,
                args=(
                    output,
                    quiet,
                    proxy,
                    speed,
                    use_cookies,
                    verify,
                    user_agent,
                    skip_download,
                    resume,
                    input_file_queue,
                ),
            )
        )
    return workers_list


def _validate_workers(workers):
    if workers is None:
        return 1

    if workers == "auto":
        return os.cpu_count() or 1

    try:
        workers = int(workers)
    except ValueError as e:
        raise ValueError(
            "Invalid value for workers: {}. Must be an integer or 'auto'.".format(
                workers
            )
        ) from e
    except TypeError as e:
        raise ValueError(
            "Invalid value for workers: {}. Must be an integer or 'auto'.".format(
                workers
            )
        ) from e

    if workers < 1:
        raise ValueError("Number of workers must be greater than 0")

    if workers > MAX_NUMBER_FILES:
        raise ValueError(
            "Number of workers must be less than or equal to {}.".format(
                MAX_NUMBER_FILES
            )
        )

    return workers


def download_folder(
    url=None,
    id=None,
    output=None,
    quiet=False,
    proxy=None,
    speed=None,
    use_cookies=True,
    remaining_ok=False,
    verify=True,
    user_agent=None,
    skip_download: bool = False,
    resume=False,
    workers=1,
) -> Union[List[str], List[GoogleDriveFileToDownload], None]:
    """Downloads entire folder from URL.

    Parameters
    ----------
    url: str
        URL of the Google Drive folder.
        Must be of the format 'https://drive.google.com/drive/folders/{url}'.
    id: str
        Google Drive's folder ID.
    output: str, optional
        String containing the path of the output folder.
        Defaults to current working directory.
    quiet: bool, optional
        Suppress terminal output.
    proxy: str, optional
        Proxy.
    speed: float, optional
        Download byte size per second (e.g., 256KB/s = 256 * 1024).
    use_cookies: bool, optional
        Flag to use cookies. Default is True.
    verify: bool or string
        Either a bool, in which case it controls whether the server's TLS
        certificate is verified, or a string, in which case it must be a path
        to a CA bundle to use. Default is True.
    user_agent: str, optional
        User-agent to use in the HTTP request.
    skip_download: bool, optional
        If True, return the list of files to download without downloading them.
        Defaults to False.
    resume: bool
        Resume interrupted transfers.
        Completed output files will be skipped.
        Partial tempfiles will be reused, if the transfer is incomplete.
        Default is False.
    workers: int or str, optional
        Number of concurrent workers to use for downloading files.
        If 'auto', it will use the number of CPU cores available.
        Default is 1.

    Returns
    -------
    files: List[str] or List[GoogleDriveFileToDownload] or None
        If dry_run is False, list of local file paths downloaded or None if failed.
        If dry_run is True, list of GoogleDriveFileToDownload that contains
        id, path, and local_path.

    Example
    -------
    gdown.download_folder(
        "https://drive.google.com/drive/folders/" +
        "1ZXEhzbLRLU1giKKRJkjm8N04cO_JoYE2",
    )
    """
    if not (id is None) ^ (url is None):
        raise ValueError("Either url or id has to be specified")
    if id is not None:
        url = "https://drive.google.com/drive/folders/{id}".format(id=id)
    if user_agent is None:
        # We need to use different user agent for folder download c.f., file
        user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36"  # NOQA: E501

    workers = _validate_workers(workers)

    sess = _get_session(proxy=proxy, use_cookies=use_cookies, user_agent=user_agent)

    if not quiet:
        print("Retrieving folder contents", file=sys.stderr)
    is_success, gdrive_file = _download_and_parse_google_drive_link(
        sess,
        url,
        quiet=quiet,
        remaining_ok=remaining_ok,
        verify=verify,
    )
    if not is_success:
        print("Failed to retrieve folder contents", file=sys.stderr)
        return None

    if not quiet:
        print("Retrieving folder contents completed", file=sys.stderr)
        print("Building directory structure", file=sys.stderr)
    directory_structure = _get_directory_structure(gdrive_file, previous_path="")
    if not quiet:
        print("Building directory structure completed", file=sys.stderr)

    if output is None:
        output = os.getcwd() + osp.sep
    if output.endswith(osp.sep):
        root_dir = osp.join(output, gdrive_file.name)
    else:
        root_dir = output
    if not skip_download and not osp.exists(root_dir):
        os.makedirs(root_dir)

    input_file_queue: queue.Queue = queue.Queue()
    files = []

    for id, path in directory_structure:
        local_path = osp.join(root_dir, path)

        if skip_download and id is not None:
            files.append(
                GoogleDriveFileToDownload(id=id, path=path, local_path=local_path)
            )
        elif id is None:
            if not osp.exists(local_path):
                os.makedirs(local_path)
            continue
        elif id is not None:
            input_file_queue.put(item=(id, path))

    if not skip_download:
        if workers > 1:
            quiet = True
        workers_threads = _create_download_workers(
            workers=workers,
            output=root_dir,
            quiet=quiet,
            proxy=proxy,
            speed=speed,
            use_cookies=use_cookies,
            verify=verify,
            user_agent=user_agent,
            skip_download=skip_download,
            resume=resume,
            input_file_queue=input_file_queue,
        )

        for worker in workers_threads:
            worker.start()

        for worker in workers_threads:
            worker.join()

        input_file_queue.join()

    return files
