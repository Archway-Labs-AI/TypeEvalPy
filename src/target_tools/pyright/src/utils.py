import os


def is_running_in_docker():
    return (
        os.path.exists("/.dockerenv")
        or os.environ.get("DOCKER_CONTAINER", False)
        or os.environ.get("DOCKER_IMAGE_NAME", False)
    )
