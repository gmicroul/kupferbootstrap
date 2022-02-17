from copy import deepcopy
import urllib.request
import tempfile
import os
import tarfile
import logging

from constants import ARCHES, BASE_DISTROS, REPOSITORIES, KUPFER_HTTPS, CHROOT_PATHS
from generator import generate_pacman_conf_body
from config import config


def resolve_url(url_template, repo_name: str, arch: str):
    result = url_template
    for template, replacement in {'$repo': repo_name, '$arch': config.runtime['arch']}.items():
        result = result.replace(template, replacement)
    return result


class PackageInfo:
    name: str
    version: str
    filename: str
    resolved_url: str

    def __init__(
        self,
        name: str,
        version: str,
        filename: str,
        resolved_url: str = None,
    ):
        self.name = name
        self.version = version
        self.filename = filename
        self.resolved_url = resolved_url

    def __repr__(self):
        return f'{self.name}@{self.version}'

    def parse_desc(desc_str: str, resolved_url=None):
        """Parses a desc file, returning a PackageInfo"""

        pruned_lines = ([line.strip() for line in desc_str.split('%') if line.strip()])
        desc = {}
        for key, value in zip(pruned_lines[0::2], pruned_lines[1::2]):
            desc[key.strip()] = value.strip()
        return PackageInfo(desc['NAME'], desc['VERSION'], desc['FILENAME'], resolved_url=resolved_url)


class RepoInfo:
    options: dict[str, str] = {}
    url_template: str

    def __init__(self, url_template: str, options: dict[str, str] = {}):
        self.url_template = url_template
        self.options.update(options)


class Repo(RepoInfo):
    name: str
    resolved_url: str
    arch: str
    packages: dict[str, PackageInfo]
    remote: bool
    scanned: bool = False

    def scan(self):
        self.resolved_url = resolve_url(self.url_template, repo_name=self.name, arch=self.arch)
        self.remote = not self.resolved_url.startswith('file://')
        uri = f'{self.resolved_url}/{self.name}.db'
        path = ''
        if self.remote:
            logging.debug(f'Downloading repo file from {uri}')
            with urllib.request.urlopen(uri) as request:
                fd, path = tempfile.mkstemp()
                with open(fd, 'wb') as writable:
                    writable.write(request.read())
        else:
            path = uri.split('file://')[1]
        logging.debug(f'Parsing repo file at {path}')
        with tarfile.open(path) as index:
            for node in index.getmembers():
                if os.path.basename(node.name) == 'desc':
                    logging.debug(f'Parsing desc file for {os.path.dirname(node.name)}')
                    pkg = PackageInfo.parse_desc(index.extractfile(node).read().decode(), self.resolved_url)
                    self.packages[pkg.name] = pkg

        self.scanned = True

    def __init__(self, name: str, url_template: str, arch: str, options={}, scan=False):
        self.packages = {}
        self.name = name
        self.url_template = url_template
        self.arch = arch
        self.options = deepcopy(options)
        if scan:
            self.scan()

    def config_snippet(self) -> str:
        options = {'Server': self.url_template} | self.options
        return ('[%s]\n' % self.name) + '\n'.join([f"{key} = {value}" for key, value in options.items()])

    def get_RepoInfo(self):
        return RepoInfo(url_template=self.url_template, options=self.options)


class Distro:
    repos: dict[str, Repo]
    arch: str

    def __init__(self, arch: str, repo_infos: dict[str, RepoInfo], scan=False):
        assert (arch in ARCHES)
        self.arch = arch
        self.repos = dict[str, Repo]()
        for repo_name, repo_info in repo_infos.items():
            self.repos[repo_name] = Repo(
                name=repo_name,
                arch=arch,
                url_template=repo_info.url_template,
                options=repo_info.options,
                scan=scan,
            )

    def get_packages(self):
        """ get packages from all repos, semantically overlaying them"""
        results = dict[str, PackageInfo]()
        for repo in self.repos.values().reverse():
            assert (repo.packages is not None)
            for package in repo.packages:
                results[package.name] = package

    def repos_config_snippet(self, extra_repos: dict[str, RepoInfo] = {}) -> str:
        extras = [Repo(name, url_template=info.url_template, arch=self.arch, options=info.options, scan=False) for name, info in extra_repos.items()]
        return '\n\n'.join(repo.config_snippet() for repo in (list(self.repos.values()) + extras))

    def get_pacman_conf(self, extra_repos: dict[str, RepoInfo] = {}, check_space: bool = True):
        body = generate_pacman_conf_body(self.arch, check_space=check_space)
        return body + self.repos_config_snippet(extra_repos)


def get_base_distro(arch: str) -> Distro:
    repos = {name: RepoInfo(url_template=url) for name, url in BASE_DISTROS[arch]['repos'].items()}
    return Distro(arch=arch, repo_infos=repos, scan=False)


def get_kupfer(arch: str, url_template: str) -> Distro:
    repos = {name: RepoInfo(url_template=url_template, options={'SigLevel': 'Never'}) for name in REPOSITORIES}
    return Distro(
        arch=arch,
        repo_infos=repos,
    )


def get_kupfer_https(arch: str) -> Distro:
    return get_kupfer(arch, KUPFER_HTTPS)


def get_kupfer_local(arch: str = None, in_chroot: bool = True) -> Distro:
    if not arch:
        arch = config.runtime['arch']
    dir = CHROOT_PATHS['packages'] if in_chroot else config.get_path('packages')
    return get_kupfer(arch, f"file://{dir}/$arch/$repo")
