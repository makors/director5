"""Serialize an approved base image and per-site package customization."""


def image_setup_for_site(site):
    image = site.docker_image
    if image is None:
        return None
    commands = list(image.setup_commands.order_by("order", "id").values_list("command", flat=True))
    if image.base_install_command.strip():
        commands.append(image.base_install_command)
    return {
        "base_image": image.name,
        "setup_commands": commands,
        "packages": site.image_packages.split(),
        "install_command_prefix": image.install_command_prefix,
        "run_script_template": image.run_script_template if site.image_write_run_script else None,
    }
