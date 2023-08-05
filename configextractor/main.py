# Main module for ConfigExtractor library
import importlib
import inspect
import os
import pkgutil
import regex
import shutil
import sys
import tempfile
import yara

from collections import defaultdict
from configextractor.frameworks import CAPE, MACO, MWCP

from logging import getLogger, Logger
from typing import Dict, List

PARSER_FRAMEWORKS = [(MACO, 'yara_rule'), (MWCP, 'yara_rule'), (CAPE, 'rule_source')]

class ConfigExtractor:
    def __init__(self, parsers_dirs: list, logger: Logger = None, parser_blocklist=[]) -> None:
        if not logger:
            logger = getLogger()
        self.log = logger
        self.FRAMEWORK_LIBRARY_MAPPING = {fw_cls.__name__: fw_cls(
            logger, yara_attr) for fw_cls, yara_attr in PARSER_FRAMEWORKS}

        self.parsers = dict()
        yara_rules = list()
        yara_rule_names = list()
        self.standalone_parsers = defaultdict(set)
        for parsers_dir in parsers_dirs:
            self.log.debug('Adding directories within parser directory in case of local dependencies')
            self.log.debug(f'Adding {os.path.join(parsers_dir, os.pardir)} to PATH')
            not_py = [file for _, _, files in os.walk(parsers_dir) for file in files
                      if not file.endswith('py') and not file.endswith('pyc')]

            # Specific feature for Assemblyline or environments wanting to run parsers from different sources
            # The goal is to try and introduce package isolation/specification similar to a virtual environment when running parsers
            local_site_packages = None
            if 'site-packages' in os.listdir(parsers_dir):
                # Found 'site-packages' directory in root of parser directory
                # Assume this is to be used for all parsers therein unless indicated otherwise
                local_site_packages = os.path.join(parsers_dir, 'site-packages')

            # Find extractors (taken from MaCo's Collector class)
            path_parent, foldername = os.path.split(parsers_dir)
            original_dir = parsers_dir
            sys.path.insert(1, path_parent)
            sys.path.insert(1, parsers_dir)
            mod = importlib.import_module(foldername)

            if mod.__file__ and not mod.__file__.startswith(parsers_dir):
                # Library confused folder name with installed package
                sys.path.remove(path_parent)
                sys.path.remove(parsers_dir)
                parsers_dir = tempfile.TemporaryDirectory().name
                shutil.copytree(original_dir, parsers_dir, dirs_exist_ok=True)

                path_parent, foldername = os.path.split(parsers_dir)
                sys.path.insert(1, path_parent)
                sys.path.insert(1, parsers_dir)
                mod = importlib.import_module(foldername)

            # walk packages in the extractors directory to find all extactors
            block_regex = regex.compile('|'.join(parser_blocklist)) if parser_blocklist else None
            for module_path, module_name, ispkg in pkgutil.walk_packages(mod.__path__, mod.__name__ + "."):

                def find_site_packages(path: str) -> str:
                    parent_dir = os.path.dirname(path)
                    if parent_dir == parsers_dir:
                        # We made it all the way back to the parser directory
                        # Use root site-packages, if any
                        return local_site_packages
                    elif 'site-packages' in os.listdir(parent_dir):
                        # We found a site-packages before going back to the root of the parser directory
                        # Assume that because it's the closest, it's the most relevant
                        return os.path.join(parent_dir, 'site-packages')
                    else:
                        # Keep searching in the parent directory for a venv
                        return find_site_packages(parent_dir)

                if ispkg:
                    # skip __init__.py
                    continue

                if module_name.endswith('.setup'):
                    # skip setup.py
                    continue

                if any([module_name.split('.')[-1] in np for np in not_py]):
                    # skip non-Python files
                    continue
                self.log.debug(f"Inspecting '{module_name}' for extractors")

                # Local site packages, if any, need to be loaded before attempting to import the module
                parser_site_packages = find_site_packages(module_path.path)
                if parser_site_packages:
                    sys.path.insert(1, parser_site_packages)
                try:
                    module = importlib.import_module(module_name)
                except Exception as e:
                    # Log if there was an error importing module
                    self.log.error(f"{module_name}: {e}")
                    continue
                finally:
                    if parser_site_packages in sys.path:
                        sys.path.remove(parser_site_packages)

                # Determine if module contains parsers of a supported framework
                candidates = [module] + [member for _,
                                         member in inspect.getmembers(module) if inspect.isclass(member)]
                for member in candidates:
                    for fw_name, fw_class in self.FRAMEWORK_LIBRARY_MAPPING.items():
                        try:
                            if fw_class.validate(member):
                                if block_regex and block_regex.match(member.__name__):
                                    continue
                                self.parsers[member.__module__] = member
                                rules = fw_class.extract_yara_from_module(member, yara_rule_names)
                                if not rules:
                                    # Standalone parser, need to know what framework to run under
                                    self.standalone_parsers[fw_name].add(member)
                                else:
                                    yara_rules.extend(rules)
                                break
                        except TypeError:
                            pass
                        except Exception as e:
                            self.log.error(f"{member}: {e}")

                # Correct metadata in YARA rules
                if original_dir != parsers_dir:
                    yara_rules = [rule.replace(parsers_dir, original_dir) for rule in yara_rules]

            if original_dir != parsers_dir:
                # Correct the paths to the parsers to match metadata changes
                sys.path.remove(path_parent)
                sys.path.remove(parsers_dir)
                path_parent, _ = os.path.split(original_dir)
                sys.path.insert(1, path_parent)
                sys.path.insert(1, original_dir)
                self.parsers = {k.replace(parsers_dir, original_dir): v for k, v in self.parsers.items()}
                shutil.rmtree(parsers_dir)

        self.yara = yara.compile(source='\n'.join(yara_rules))
        self.log.debug(f"# of YARA-dependent parsers: {len(self.parsers)}")
        self.log.debug(f"# of YARA rules extracted from parsers: {len(yara_rules)}")
        [self.log.debug(f"# of standalone {k} parsers: {len(v)}") for k, v in self.standalone_parsers.items()]
        if parser_blocklist:
            self.log.info(f"Ignoring output from the following parsers matching: {parser_blocklist}")

    def get_details(self, parser_path) -> Dict[str, str]:
        # Determine framework
        module = self.parsers[parser_path]
        for fw_name, fw_class in self.FRAMEWORK_LIBRARY_MAPPING.items():
            if fw_class.validate(module):
                # Extract details about parser
                return {
                    'framework': fw_name,
                    'classification': fw_class.__class__.get_classification(module),
                    'name': fw_class.__class__.get_name(module)
                }
        return None

    def finalize(self, results: List[dict]):
        # Ensure schemes/protocol are present in HTTP configurations
        for config in results.values():
            config = config.get('config', {})
            for network_conn in config.get('http', []):
                network_conn.setdefault('protocol', 'http')
                uri: str = network_conn.get('uri')
                if uri and not uri.startswith(network_conn['protocol']):
                    # Ensure URI starts with protocol
                    network_conn['uri'] = f"{network_conn['protocol']}://{uri}"

    def run_parsers(self, sample, parser_blocklist=[]):
        results = dict()
        parsers_to_run = defaultdict(lambda: defaultdict(list))
        parser_names = list()
        block_regex = regex.compile('|'.join(parser_blocklist)) if parser_blocklist else None

        # Get YARA-dependents parsers that should run based on match
        for yara_match in self.yara.match(sample):
            # Retrieve relevant parser information
            parser_module = yara_match.meta.get('parser_module')
            parser_framework = yara_match.meta.get('parser_framework')
            parser_names.append(yara_match.meta.get('parser_name'))

            parser_module = self.parsers[parser_module]
            if block_regex and block_regex.match(parser_module.__name__):
                self.log.info(f'Blocking {parser_module.__name__} based on passed blocklist regex list')
                continue
            # Pass in yara.Match objects since some framework can leverage it
            parsers_to_run[parser_framework][parser_module].append(yara_match)

        # Add standalone parsers that should run on any file
        for parser_framework, parser_list in self.standalone_parsers.items():
            for parser in parser_list:
                if block_regex and block_regex.match(parser.__name__):
                    self.log.info(f'Blocking {parser.__name__} based on passed blocklist regex list')
                    continue
                parsers_to_run[parser_framework][parser].extend([])

        for framework, parser_list in parsers_to_run.items():
            if parser_list:
                self.log.debug(f'Running the following under the {framework} framework with YARA: {parser_names}')
                result = self.FRAMEWORK_LIBRARY_MAPPING[framework].run(sample, parser_list)
                self.finalize(result)
                if result:
                    results[framework] = result

        return results
