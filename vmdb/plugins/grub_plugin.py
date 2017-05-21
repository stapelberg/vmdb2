# Copyright 2017  Lars Wirzenius
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# =*= License: GPL-3+ =*=


# Installing GRUB onto a disk image is a bit of a black art. I haven't
# found any good documentation for it. This plugin is written based on
# de-ciphering the build_openstack_image script. Here is an explanation
# of what I _THINK_ is happening.
#
# The crucial command is grub-install. It needs a ton of options to
# work correctly: see below in the code for the list, and the manpage
# for an explanation of what each of them means. We will be running
# grub-install in a chroot so that we use the version in the Debian
# version we're installing, rather than the host system, which might
# be any Debian version.
#
# To run grub-install in a chroot, we need to set up the chroot in
# various ways. Firstly, we need to tell grub-install which device
# file the image has. We can't just give it the image file itself,
# since it isn't inside the chroot, so instead we arrange to have a
# loop block device that covers the whole image file, and we bind
# mount /dev into the chroot so the device is available.
#
# grub-install seems to also require /proc and /sys so we bind mount
# those into the chroot as well.
#
# We install the UEFI version of GRUB, and for that we additionally
# bind mount the EFI partition in the image. Oh yeah, you MUST have
# one.
#
# We also make sure the right GRUB package is installed in the chroot,
# before we run grub-install.
#
# Further, there's some configuration tweaking we need to do. See the
# code. Don't ask me why they're necessary.
#
# For cleanliness, we also undo any bind mounts into the chroot. Don't
# want to leave them in case they cause trouble.
#
# Note that this is currently rather strongly assuming that UEFI and
# the amd64 (a.k.a. x86_64) architecture are being used. These should
# probably not be hardcoded. Patch welcome.

# To use this plugin: write steps to create a root filesystem, and an
# VFAT filesystem to be mounted as /boot/efi. Install Debian onto the
# root filesystem. Then install grub with a step like this:
#
#         - grub: uefi
#           device: "{{ output }}"
#           root-fs: root-fs
#           root-part: root-part
#           efi-part: efi-part
#
# Here: device specifies the output image, root-fs is the tag for the
# root filesystem, root-part is the tag for the partition with the
# root filesystem, and efi-part is tag for the EFI partition.
#
# The grub step will take of the rest.


import logging
import os
import re
import sys

import cliapp

import vmdb


class GrubPlugin(cliapp.Plugin):

    def enable(self):
        self.app.step_runners.add(GrubStepRunner())


class GrubStepRunner(vmdb.StepRunnerInterface):

    def get_required_keys(self):
        return ['grub', 'root-fs']

    def run(self, step, settings, state):
        flavor = step['grub']
        assert flavor == 'uefi'

        grub_package = 'grub-efi-amd64'
        grub_target = 'x86_64-efi'

        device = step['device']

        rootfs = step['root-fs']
        chroot = state.mounts[rootfs]

        root_part = step['root-part']
        root_dev = state.parts[root_part]

        efi_part = step['efi-part']
        efi_dev = state.parts[efi_part]

        image_dev = self.get_image_loop_device(root_dev)

        self.bind_mount_many(chroot, ['/dev', '/proc', '/sys'], state)
        self.mount(chroot, efi_dev, '/boot/efi', state)

        self.install_package(chroot, grub_package)

        kernel_params = [
            'biosdevname=0',
            'net.ifnames=0',
            'consoleblank=0',
            'systemd.show_status=true',
        ]
        self.set_grub_cmdline_config(chroot, kernel_params)

        self.chroot(chroot, ['grub-mkconfig', '-o', '/boot/grub/grub.cfg'])
        self.chroot(
            chroot, [
                'grub-install',
                '--target=' + grub_target,
                '--no-nvram',
                '--force-extra-removable',
                '--no-floppy',
                '--modules=part_msdos part_gpt',
                '--grub-mkdevicemap=/boot/grub/device.map',
                image_dev,
            ]
        )

        self.unmount(state)

    def teardown(self, step, settings, state):
        self.unmount(state)

    def unmount(self, state):
        mounts = getattr(state, 'grub_mounts', [])
        mounts.reverse()
        while mounts:
            mount_point = mounts.pop()
            cliapp.runcmd(['umount', mount_point])

    def get_image_loop_device(self, partition_device):
        # We get /dev/mappers/loopXpY and return /dev/loopX
        assert partition_device.startswith('/dev/mapper/loop')

        m = re.match('^/dev/mapper/(?P<loop>loop\d+)p\d+$', partition_device)
        assert m is not None

        loop = m.group('loop')
        return '/dev/{}'.format(loop)

    def bind_mount_many(self, chroot, paths, state):
        for path in paths:
            self.mount(chroot, path, path, state, mount_opts=['--bind'])

    def mount(self, chroot, path, mount_point, state, mount_opts=None):
        chroot_path = self.chroot_path(chroot, mount_point)
        if not os.path.exists(chroot_path):
            os.makedirs(chroot_path)

        if mount_opts is None:
            mount_opts = []

        cliapp.runcmd(['mount'] + mount_opts + [path, chroot_path])

        binds = getattr(state, 'grub_mounts', None)
        if binds is None:
            binds = []
        binds.append(chroot_path)
        state.grub_mounts = binds

    def chroot_path(self, chroot, path):
        return os.path.normpath(os.path.join(chroot, '.' + path))

    def install_package(self, chroot, package):
        self.chroot(
            chroot, 
            ['apt-get', '-y', '--no-show-progress', 'install', package])

    def chroot(self, chroot, argv, **kwargs):
        return cliapp.runcmd(['chroot', chroot] + argv, **kwargs)

    def set_grub_cmdline_config(self, chroot, kernel_params):
        param_string = ' '.join(kernel_params)

        filename = self.chroot_path(chroot, '/etc/default/grub')

        with open(filename) as f:
            text = f.read()

        lines = text.splitlines()
        lines = [line for line in lines
                 if not line.startswith('GRUB_CMDLINE_LINUX_DEFAULT')]
        lines.append('GRUB_CMDLINE_LINUX_DEFAULT="{}"'.format(param_string))

        with open(filename, 'w') as f:
            f.write('\n'.join(lines) + '\n')