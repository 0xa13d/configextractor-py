import click
import logging
import json
import os

from configextractor.main import ConfigExtractor


@click.command()
@click.option('-p', '--parsers_paths', help="Directory containing parsers",  type=click.Path(exists=True), multiple=True)
@click.option('-s', '--sample_paths', help="Path to samples", type=click.Path(exists=True), multiple=True)
@click.option('-b', '--block', help="Parser to ignore based on regex pattern",  type=click.STRING, default=[], multiple=True)
@click.option('-v', '--verbosity', help="Logging verbosity",
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
              default="WARNING")
def main(parsers_paths, sample_paths, block, verbosity) -> None:
    logger = logging.getLogger('cx')
    logger.setLevel(verbosity)
    logger.addHandler(logging.StreamHandler())
    cx = ConfigExtractor(parsers_paths, parser_blocklist=block, logger=logger)

    # Check if path given is a directory or a file
    results = dict()
    for sample_path in sample_paths:
        if os.path.isfile(sample_path):
            results = cx.run_parsers(sample_path)
        else:
            results = dict()
            # Iterate over directory
            for root, _, files in os.walk(sample_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    result = cx.run_parsers(file_path)
                    if result:
                        results[file_path] = result

    print("Results:")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
