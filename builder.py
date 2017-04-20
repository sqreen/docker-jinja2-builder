from __future__ import print_function

import sys
import argparse
import itertools
import sys
import yaml
import tarfile
from docker import DockerClient
from docker.utils import exclude_paths
from io import BytesIO
import tempfile
from os.path import join, exists, expanduser, basename
from jinja2 import Template
from hashlib import sha1


DOCKER = DockerClient()


class BuildingException(Exception):

    def __init__(self, error):
        self.error = error


def prepare_string_for_tar(name, content):
    dfinfo = tarfile.TarInfo(name)
    bytesio = BytesIO(content.encode('utf-8'))
    dfinfo.size = len(bytesio.getvalue())
    bytesio.seek(0)
    return dfinfo, bytesio


def docker_context(dockerfile_content, base_path, options):
    context = tempfile.NamedTemporaryFile()

    archive = tarfile.open(mode='w', fileobj=context)

    archive.addfile(*prepare_string_for_tar('Dockerfile', dockerfile_content))

    root = base_path

    # Process dockerignore
    dockerignore = join(root, '.dockerignore')
    exclude = None
    if exists(dockerignore):
        with open(dockerignore, 'r') as f:
            exclude = list(filter(bool, f.read().splitlines()))

    # Clean patterns
    exclude = clean_dockerignore(exclude)

    # Add root directory
    for path in sorted(exclude_paths(root, exclude)):
        archive.add(join(root, path), arcname=path, recursive=False)

    # Process options to check if we should includes a file outside the root directory
    for option in options.values():
        if option['value']:
            include_file = option['def'].get('include_file', False)
            if include_file:
                archive.add(option['def']['local_path'], arcname=option['value'], recursive=False)

    archive.close()
    context.seek(0)

    return context


def clean_dockerignore(patterns):
    final_patterns = []

    for pattern in patterns:
        if '**' in pattern:
            final_patterns.extend(dir_wildcard_workaround(pattern))
        else:
            final_patterns.append(pattern)

    return final_patterns


def dir_wildcard_workaround(pattern):
    header, tail = pattern.split('**')
    tail = tail.lstrip('/')
    header = header.rstrip('/')

    for i in range(20):
        level = ['*'] * i

        path = [header] + level + [tail]
        yield join(*path)


def build(context, tag):
    stream = DOCKER.api.build(custom_context=True, fileobj=context, tag=tag,
                              stream=True, decode=True)
    for log_line in stream:

        # Check for errors
        if log_line.get('error', None):
            raise BuildingException(log_line)

        # Save the log line in case next one is an error
        if log_line:
            print(log_line.get('stream'), end="")


def get_image_name(combination, matrix):
    formatted_image_id = matrix['image_id'].format(**combination)
    image_id = sha1(formatted_image_id.encode('utf8')).hexdigest()
    return matrix['image_name'].format(ID=image_id)


def is_blacklisted(combination, blacklist):
    combination_as_set = set(combination.items())
    for blacklisted_combination in blacklist:
        blacklisted_combination_as_set = set(blacklisted_combination.items())
        if blacklisted_combination_as_set.issubset(combination_as_set):
            return True
    return False


def get_all_combinations(matrix, blacklist):
    """
    >>> list(get_all_combinations(dict(number=[1,2], character='ab')))
    [{'character': 'a', 'number': 1},
     {'character': 'a', 'number': 2},
     {'character': 'b', 'number': 1},
     {'character': 'b', 'number': 2}]
    """
    result = []

    for combination in itertools.product(*matrix.values()):
        combination = dict(zip(matrix, combination))

        if is_blacklisted(combination, blacklist):
            print("Combination {} is blacklisted, ignore it".format(combination))
            continue

        result.append(combination)
    return result


def build_all_combinations(matrix, template, base_path, options):
    template = Template(template)
    combinations = list(get_all_combinations(matrix['matrix'], matrix.get('blacklist')))

    for combination in combinations:
        dockerfile = template.render(options=options, **combination)
        context = docker_context(dockerfile, base_path, options)

        image_name = get_image_name(combination, matrix)

        try:
            build(context, image_name)
        except BuildingException as e:
            err_msg = "Failing to build image {} for combination {} because of error {}"
            print(err_msg.format(image_name, combination, e.error))
        else:
            print("Image {} successfully built for combination {}".format(image_name, combination))


def main():
    parser = argparse.ArgumentParser(
        description='Build multiple docker images with Jinja2',
        add_help=False)
    parser.add_argument(
        'base_path',
        action='store',
        help='A directory where a matrix.yml file and a Dockerfile.jinja2 can be find')
    parser.add_argument(
        '--help', '-h',
        action="store_true",
        help='Show this help message and exit')

    base_path = parser.parse_known_args()[0].base_path

    # Read base paths
    with open(join(base_path, 'matrix.yml')) as matrix_file:
        matrix = yaml.load(matrix_file.read())

    with open(join(base_path, 'Dockerfile.jinja2')) as template_file:
        template = template_file.read()

    # Get options for updating the arg parser
    options = matrix.get('options', {})
    for option_name, option in options.items():
        # Check if it the option is a file
        if option and 'include_file' in option:
            parser.add_argument(
                '--{}'.format(option_name),
                dest=option_name,
                action='store'
            )
        else:
            parser.add_argument(
                '--{}'.format(option_name),
                dest=option_name,
                action='store_true',
            )

    # Parse it really this time
    args = parser.parse_args()
    if args.help is True:
        sys.exit(parser.print_help())

    # Recover options
    final_options = {}
    for option_name, option in options.items():
        arg_value = getattr(args, option_name)

        final_options[option_name] = {}
        if option and option.get('include_file', False) and arg_value:
            final_options[option_name]['value'] = basename(expanduser(arg_value))
            final_options[option_name]['def'] = option
            final_options[option_name]['def']['local_path'] = expanduser(arg_value)
        else:
            final_options[option_name]['value'] = arg_value
            final_options[option_name]['def'] = option

    build_all_combinations(matrix, template, base_path, final_options)

if __name__ == '__main__':
    main()
