# Copyright (c) 2013 VMware, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Implements operations on volumes residing on VMware datastores.
"""

from cinder.openstack.common.gettextutils import _
from cinder.openstack.common import log as logging
from cinder.openstack.common import units
from cinder.volume.drivers.vmware import error_util
from cinder.volume.drivers.vmware import vim_util

LOG = logging.getLogger(__name__)
LINKED_CLONE_TYPE = 'linked'
FULL_CLONE_TYPE = 'full'
ALREADY_EXISTS = 'AlreadyExists'
FILE_ALREADY_EXISTS = 'FileAlreadyExists'


def split_datastore_path(datastore_path):
    """Split the datastore path to components.

    return the datastore name, relative folder path and the file name

    E.g. datastore_path = [datastore1] my_volume/my_volume.vmdk, returns
    (datastore1, my_volume/, my_volume.vmdk)

    :param datastore_path: Datastore path of a file
    :return: Parsed datastore name, relative folder path and file name
    """
    splits = datastore_path.split('[', 1)[1].split(']', 1)
    datastore_name = None
    folder_path = None
    file_name = None
    if len(splits) == 1:
        datastore_name = splits[0]
    else:
        datastore_name, path = splits
        # Path will be of form my_volume/my_volume.vmdk
        # we need into my_volumes/ and my_volume.vmdk
        splits = path.split('/')
        file_name = splits[len(splits) - 1]
        folder_path = path[:-len(file_name)]

    return (datastore_name.strip(), folder_path.strip(), file_name.strip())


class VirtualDiskPath(object):
    """Class representing paths of files comprising a virtual disk."""

    def __init__(self, ds_name, folder_path, disk_name):
        """Creates path object for the given disk.

        :param ds_name: name of the datastore where disk is stored
        :param folder_path: absolute path of the folder containing the disk
        :param disk_name: name of the virtual disk
        """
        self._descriptor_file_path = "%s%s.vmdk" % (folder_path, disk_name)
        self._descriptor_ds_file_path = self.get_datastore_file_path(
            ds_name, self._descriptor_file_path)

    def get_datastore_file_path(self, ds_name, file_path):
        """Get datastore path corresponding to the given file path.

        :param ds_name: name of the datastore containing the file represented
                        by the given file path
        :param file_path: absolute path of the file
        :return: datastore file path
        """
        return "[%s] %s" % (ds_name, file_path)

    def get_descriptor_file_path(self):
        """Get absolute file path of the virtual disk descriptor."""
        return self._descriptor_file_path

    def get_descriptor_ds_file_path(self):
        """Get datastore file path of the virtual disk descriptor."""
        return self._descriptor_ds_file_path


class FlatExtentVirtualDiskPath(VirtualDiskPath):
    """Paths of files in a non-monolithic disk with a single flat extent."""

    def __init__(self, ds_name, folder_path, disk_name):
        """Creates path object for the given disk.

        :param ds_name: name of the datastore where disk is stored
        :param folder_path: absolute path of the folder containing the disk
        :param disk_name: name of the virtual disk
        """
        super(FlatExtentVirtualDiskPath, self).__init__(
            ds_name, folder_path, disk_name)
        self._flat_extent_file_path = "%s%s-flat.vmdk" % (folder_path,
                                                          disk_name)
        self._flat_extent_ds_file_path = self.get_datastore_file_path(
            ds_name, self._flat_extent_file_path)

    def get_flat_extent_file_path(self):
        """Get absolute file path of the flat extent."""
        return self._flat_extent_file_path

    def get_flat_extent_ds_file_path(self):
        """Get datastore file path of the flat extent."""
        return self._flat_extent_ds_file_path


class MonolithicSparseVirtualDiskPath(VirtualDiskPath):
    """Paths of file comprising a monolithic sparse disk."""
    pass


class VirtualDiskType(object):
    """Supported virtual disk types."""

    EAGER_ZEROED_THICK = "eagerZeroedThick"
    PREALLOCATED = "preallocated"
    THIN = "thin"

    # thick in extra_spec means lazy-zeroed thick disk
    EXTRA_SPEC_DISK_TYPE_DICT = {'eagerZeroedThick': EAGER_ZEROED_THICK,
                                 'thick': PREALLOCATED,
                                 'thin': THIN
                                 }

    @staticmethod
    def is_valid(extra_spec_disk_type):
        """Check if the given disk type in extra_spec is valid.

        :param extra_spec_disk_type: disk type in extra_spec
        :return: True if valid
        """
        return (extra_spec_disk_type in
                VirtualDiskType.EXTRA_SPEC_DISK_TYPE_DICT)

    @staticmethod
    def validate(extra_spec_disk_type):
        """Validate the given disk type in extra_spec.

        This method throws an instance of InvalidDiskTypeException if the given
        disk type is invalid.

        :param extra_spec_disk_type: disk type in extra_spec
        :raises: InvalidDiskTypeException
        """
        if not VirtualDiskType.is_valid(extra_spec_disk_type):
            raise error_util.InvalidDiskTypeException(
                disk_type=extra_spec_disk_type)

    @staticmethod
    def get_virtual_disk_type(extra_spec_disk_type):
        """Return disk type corresponding to the extra_spec disk type.

        :param extra_spec_disk_type: disk type in extra_spec
        :return: virtual disk type
        :raises: InvalidDiskTypeException
        """
        VirtualDiskType.validate(extra_spec_disk_type)
        return (VirtualDiskType.EXTRA_SPEC_DISK_TYPE_DICT[
                extra_spec_disk_type])


class VirtualDiskAdapterType(object):
    """Supported virtual disk adapter types."""

    LSI_LOGIC = "lsiLogic"
    BUS_LOGIC = "busLogic"
    LSI_LOGIC_SAS = "lsiLogicsas"
    IDE = "ide"

    @staticmethod
    def is_valid(adapter_type):
        """Check if the given adapter type is valid.

        :param adapter_type: adapter type to check
        :return: True if valid
        """
        return adapter_type in [VirtualDiskAdapterType.LSI_LOGIC,
                                VirtualDiskAdapterType.BUS_LOGIC,
                                VirtualDiskAdapterType.LSI_LOGIC_SAS,
                                VirtualDiskAdapterType.IDE]

    @staticmethod
    def validate(extra_spec_adapter_type):
        """Validate the given adapter type in extra_spec.

        This method throws an instance of InvalidAdapterTypeException if the
        given adapter type is invalid.

        :param extra_spec_adapter_type: adapter type in extra_spec
        :raises: InvalidAdapterTypeException
        """
        if not VirtualDiskAdapterType.is_valid(extra_spec_adapter_type):
            raise error_util.InvalidAdapterTypeException(
                invalid_type=extra_spec_adapter_type)

    @staticmethod
    def get_adapter_type(extra_spec_adapter_type):
        """Get the adapter type to be used in VirtualDiskSpec.

        :param extra_spec_adapter_type: adapter type in the extra_spec
        :return: adapter type to be used in VirtualDiskSpec
        """
        VirtualDiskAdapterType.validate(extra_spec_adapter_type)
        # We set the adapter type as lsiLogic for lsiLogicsas since it is not
        # supported by VirtualDiskManager APIs. This won't be a problem because
        # we attach the virtual disk to the correct controller type and the
        # disk adapter type is always resolved using its controller key.
        if extra_spec_adapter_type == VirtualDiskAdapterType.LSI_LOGIC_SAS:
            return VirtualDiskAdapterType.LSI_LOGIC
        return extra_spec_adapter_type


class ControllerType(object):
    """Encapsulate various controller types."""

    LSI_LOGIC = 'VirtualLsiLogicController'
    BUS_LOGIC = 'VirtualBusLogicController'
    LSI_LOGIC_SAS = 'VirtualLsiLogicSASController'
    IDE = 'VirtualIDEController'

    CONTROLLER_TYPE_DICT = {
        VirtualDiskAdapterType.LSI_LOGIC: LSI_LOGIC,
        VirtualDiskAdapterType.BUS_LOGIC: BUS_LOGIC,
        VirtualDiskAdapterType.LSI_LOGIC_SAS: LSI_LOGIC_SAS,
        VirtualDiskAdapterType.IDE: IDE}

    @staticmethod
    def get_controller_type(adapter_type):
        """Get the disk controller type based on the given adapter type.

        :param adapter_type: disk adapter type
        :return: controller type corresponding to the given adapter type
        :raises: InvalidAdapterTypeException
        """
        if adapter_type in ControllerType.CONTROLLER_TYPE_DICT:
            return ControllerType.CONTROLLER_TYPE_DICT[adapter_type]
        raise error_util.InvalidAdapterTypeException(invalid_type=adapter_type)

    @staticmethod
    def is_scsi_controller(controller_type):
        """Check if the given controller is a SCSI controller.

        :param controller_type: controller type
        :return: True if the controller is a SCSI controller
        """
        return controller_type in [ControllerType.LSI_LOGIC,
                                   ControllerType.BUS_LOGIC,
                                   ControllerType.LSI_LOGIC_SAS]


class VMwareVolumeOps(object):
    """Manages volume operations."""

    def __init__(self, session, max_objects):
        self._session = session
        self._max_objects = max_objects

    def get_backing(self, name):
        """Get the backing based on name.

        :param name: Name of the backing
        :return: Managed object reference to the backing
        """

        retrieve_result = self._session.invoke_api(vim_util, 'get_objects',
                                                   self._session.vim,
                                                   'VirtualMachine',
                                                   self._max_objects)
        while retrieve_result:
            vms = retrieve_result.objects
            for vm in vms:
                if vm.propSet[0].val == name:
                    # We got the result, so cancel further retrieval.
                    self.cancel_retrieval(retrieve_result)
                    return vm.obj
            # Result not obtained, continue retrieving results.
            retrieve_result = self.continue_retrieval(retrieve_result)

        LOG.debug("Did not find any backing with name: %s" % name)

    def delete_backing(self, backing):
        """Delete the backing.

        :param backing: Managed object reference to the backing
        """
        LOG.debug("Deleting the VM backing: %s." % backing)
        task = self._session.invoke_api(self._session.vim, 'Destroy_Task',
                                        backing)
        LOG.debug("Initiated deletion of VM backing: %s." % backing)
        self._session.wait_for_task(task)
        LOG.info(_("Deleted the VM backing: %s.") % backing)

    # TODO(kartikaditya) Keep the methods not specific to volume in
    # a different file
    def get_host(self, instance):
        """Get host under which instance is present.

        :param instance: Managed object reference of the instance VM
        :return: Host managing the instance VM
        """
        return self._session.invoke_api(vim_util, 'get_object_property',
                                        self._session.vim, instance,
                                        'runtime.host')

    def get_hosts(self):
        """Get all host from the inventory.

        :return: All the hosts from the inventory
        """
        return self._session.invoke_api(vim_util, 'get_objects',
                                        self._session.vim,
                                        'HostSystem', self._max_objects)

    def continue_retrieval(self, retrieve_result):
        """Continue retrieval of results if necessary.

        :param retrieve_result: Result from RetrievePropertiesEx
        """

        return self._session.invoke_api(vim_util, 'continue_retrieval',
                                        self._session.vim, retrieve_result)

    def cancel_retrieval(self, retrieve_result):
        """Cancel retrieval of results if necessary.

        :param retrieve_result: Result from RetrievePropertiesEx
        """

        self._session.invoke_api(vim_util, 'cancel_retrieval',
                                 self._session.vim, retrieve_result)

    def _is_usable(self, mount_info):
        """Check if a datastore is usable as per the given mount info.

        The datastore is considered to be usable for a host only if it is
        writable, mounted and accessible.

        :param mount_info: Host mount information
        :return: True if datastore is usable
        """
        writable = mount_info.accessMode == 'readWrite'
        # If mounted attribute is not set, then default is True
        mounted = getattr(mount_info, 'mounted', True)
        # If accessible attribute is not set, then default is False
        accessible = getattr(mount_info, 'accessible', False)

        return writable and mounted and accessible

    def get_connected_hosts(self, datastore):
        """Get all the hosts to which the datastore is connected and usable.

        The datastore is considered to be usable for a host only if it is
        writable, mounted and accessible.

        :param datastore: Reference to the datastore entity
        :return: List of managed object references of all connected
                 hosts
        """
        summary = self.get_summary(datastore)
        if not summary.accessible:
            return []

        host_mounts = self._session.invoke_api(vim_util, 'get_object_property',
                                               self._session.vim, datastore,
                                               'host')
        if not hasattr(host_mounts, 'DatastoreHostMount'):
            return []

        connected_hosts = []
        for host_mount in host_mounts.DatastoreHostMount:
            if self._is_usable(host_mount.mountInfo):
                connected_hosts.append(host_mount.key.value)

        return connected_hosts

    def _in_maintenance(self, summary):
        """Check if a datastore is entering maintenance or in maintenance.

        :param summary: Summary information about the datastore
        :return: True if the datastore is entering maintenance or in
                 maintenance
        """
        if hasattr(summary, 'maintenanceMode'):
            return summary.maintenanceMode in ['enteringMaintenance',
                                               'inMaintenance']
        return False

    def _is_valid(self, datastore, host):
        """Check if the datastore is valid for the given host.

        A datastore is considered valid for a host only if the datastore is
        writable, mounted and accessible. Also, the datastore should not be
        in maintenance mode.

        :param datastore: Reference to the datastore entity
        :param host: Reference to the host entity
        :return: True if datastore can be used for volume creation
        """
        summary = self.get_summary(datastore)
        in_maintenance = self._in_maintenance(summary)
        if not summary.accessible or in_maintenance:
            return False

        host_mounts = self._session.invoke_api(vim_util, 'get_object_property',
                                               self._session.vim, datastore,
                                               'host')
        for host_mount in host_mounts.DatastoreHostMount:
            if host_mount.key.value == host.value:
                return self._is_usable(host_mount.mountInfo)
        return False

    def get_dss_rp(self, host):
        """Get accessible datastores and resource pool of the host.

        :param host: Managed object reference of the host
        :return: Datastores accessible to the host and resource pool to which
                 the host belongs to
        """

        props = self._session.invoke_api(vim_util, 'get_object_properties',
                                         self._session.vim, host,
                                         ['datastore', 'parent'])
        # Get datastores and compute resource or cluster compute resource
        datastores = []
        compute_resource = None
        for elem in props:
            for prop in elem.propSet:
                if prop.name == 'datastore' and prop.val:
                    # Consider only if datastores are present under host
                    datastores = prop.val.ManagedObjectReference
                elif prop.name == 'parent':
                    compute_resource = prop.val
        LOG.debug("Datastores attached to host %(host)s are: %(ds)s.",
                  {'host': host, 'ds': datastores})
        # Filter datastores based on if it is accessible, mounted and writable
        valid_dss = []
        for datastore in datastores:
            if self._is_valid(datastore, host):
                valid_dss.append(datastore)
        # Get resource pool from compute resource or cluster compute resource
        resource_pool = self._session.invoke_api(vim_util,
                                                 'get_object_property',
                                                 self._session.vim,
                                                 compute_resource,
                                                 'resourcePool')
        if not valid_dss:
            msg = _("There are no valid datastores attached to %s.") % host
            LOG.error(msg)
            raise error_util.VimException(msg)
        else:
            LOG.debug("Valid datastores are: %s", valid_dss)
        return (valid_dss, resource_pool)

    def _get_parent(self, child, parent_type):
        """Get immediate parent of given type via 'parent' property.

        :param child: Child entity reference
        :param parent_type: Entity type of the parent
        :return: Immediate parent of specific type up the hierarchy via
                 'parent' property
        """

        if not child:
            return None
        if child._type == parent_type:
            return child
        parent = self._session.invoke_api(vim_util, 'get_object_property',
                                          self._session.vim, child, 'parent')
        return self._get_parent(parent, parent_type)

    def get_dc(self, child):
        """Get parent datacenter up the hierarchy via 'parent' property.

        :param child: Reference of the child entity
        :return: Parent Datacenter of the param child entity
        """
        return self._get_parent(child, 'Datacenter')

    def get_vmfolder(self, datacenter):
        """Get the vmFolder.

        :param datacenter: Reference to the datacenter entity
        :return: vmFolder property of the datacenter
        """
        return self._session.invoke_api(vim_util, 'get_object_property',
                                        self._session.vim, datacenter,
                                        'vmFolder')

    def create_folder(self, parent_folder, child_folder_name):
        """Creates child folder with given name under the given parent folder.

        The method first checks if a child folder already exists, if it does,
        then it returns a moref for the folder, else it creates one and then
        return the moref.

        :param parent_folder: Reference to the folder entity
        :param child_folder_name: Name of the child folder
        :return: Reference to the child folder with input name if it already
                 exists, else create one and return the reference
        """
        LOG.debug("Creating folder: %(child_folder_name)s under parent "
                  "folder: %(parent_folder)s." %
                  {'child_folder_name': child_folder_name,
                   'parent_folder': parent_folder})

        # Get list of child entities for the parent folder
        prop_val = self._session.invoke_api(vim_util, 'get_object_property',
                                            self._session.vim, parent_folder,
                                            'childEntity')
        child_entities = prop_val.ManagedObjectReference

        # Return if the child folder with input name is already present
        for child_entity in child_entities:
            if child_entity._type != 'Folder':
                continue
            child_entity_name = self.get_entity_name(child_entity)
            if child_entity_name == child_folder_name:
                LOG.debug("Child folder already present: %s." %
                          child_entity)
                return child_entity

        # Need to create the child folder
        child_folder = self._session.invoke_api(self._session.vim,
                                                'CreateFolder', parent_folder,
                                                name=child_folder_name)
        LOG.debug("Created child folder: %s." % child_folder)
        return child_folder

    def extend_virtual_disk(self, requested_size_in_gb, name, dc_ref,
                            eager_zero=False):
        """Extend the virtual disk to the requested size.

        :param requested_size_in_gb: Size of the volume in GB
        :param name: Name of the backing
        :param dc_ref: Reference datacenter
        :param eager_zero: Boolean determining if the free space
        is zeroed out
        """
        LOG.debug("Extending the volume %(name)s to %(size)s GB.",
                  {'name': name, 'size': requested_size_in_gb})
        diskMgr = self._session.vim.service_content.virtualDiskManager

        # VMWare API needs the capacity unit to be in KB, so convert the
        # capacity unit from GB to KB.
        size_in_kb = requested_size_in_gb * units.Mi
        task = self._session.invoke_api(self._session.vim,
                                        "ExtendVirtualDisk_Task",
                                        diskMgr,
                                        name=name,
                                        datacenter=dc_ref,
                                        newCapacityKb=size_in_kb,
                                        eagerZero=eager_zero)
        self._session.wait_for_task(task)
        LOG.info(_("Successfully extended the volume %(name)s to "
                   "%(size)s GB."),
                 {'name': name, 'size': requested_size_in_gb})

    def _create_specs_for_disk_add(self, size_kb, disk_type, adapter_type):
        """Create controller and disk specs for adding a new disk.

        :param size_kb: disk size in KB
        :param disk_type: disk provisioning type
        :param adapter_type: disk adapter type
        :return: list containing controller and disk specs
        """
        cf = self._session.vim.client.factory
        controller_type = ControllerType.get_controller_type(adapter_type)
        controller_device = cf.create('ns0:%s' % controller_type)
        controller_device.key = -100
        controller_device.busNumber = 0
        if ControllerType.is_scsi_controller(controller_type):
            controller_device.sharedBus = 'noSharing'
        controller_spec = cf.create('ns0:VirtualDeviceConfigSpec')
        controller_spec.operation = 'add'
        controller_spec.device = controller_device

        disk_device = cf.create('ns0:VirtualDisk')
        # for very small disks allocate at least 1KB
        disk_device.capacityInKB = max(1, int(size_kb))
        disk_device.key = -101
        disk_device.unitNumber = 0
        disk_device.controllerKey = -100
        disk_device_bkng = cf.create('ns0:VirtualDiskFlatVer2BackingInfo')
        if disk_type == 'eagerZeroedThick':
            disk_device_bkng.eagerlyScrub = True
        elif disk_type == 'thin':
            disk_device_bkng.thinProvisioned = True
        disk_device_bkng.fileName = ''
        disk_device_bkng.diskMode = 'persistent'
        disk_device.backing = disk_device_bkng
        disk_spec = cf.create('ns0:VirtualDeviceConfigSpec')
        disk_spec.operation = 'add'
        disk_spec.fileOperation = 'create'
        disk_spec.device = disk_device

        return [controller_spec, disk_spec]

    def _get_create_spec_disk_less(self, name, ds_name, profileId=None):
        """Return spec for creating disk-less backing.

        :param name: Name of the backing
        :param ds_name: Datastore name where the disk is to be provisioned
        :param profileId: storage profile ID for the backing
        :return: Spec for creation
        """
        cf = self._session.vim.client.factory
        vm_file_info = cf.create('ns0:VirtualMachineFileInfo')
        vm_file_info.vmPathName = '[%s]' % ds_name

        create_spec = cf.create('ns0:VirtualMachineConfigSpec')
        create_spec.name = name
        create_spec.guestId = 'otherGuest'
        create_spec.numCPUs = 1
        create_spec.memoryMB = 128
        create_spec.files = vm_file_info
        # set the Hardware version to the lowest version supported by ESXi5.0
        # and compatible with vCenter Server 5.0
        # This ensures migration of volume created on a later ESX server
        # works on any ESX server 5.0 and above.
        create_spec.version = "vmx-08"

        if profileId:
            vmProfile = cf.create('ns0:VirtualMachineDefinedProfileSpec')
            vmProfile.profileId = profileId
            create_spec.vmProfile = [vmProfile]

        return create_spec

    def get_create_spec(self, name, size_kb, disk_type, ds_name,
                        profileId=None, adapter_type='lsiLogic'):
        """Return spec for creating backing with a single disk.

        :param name: name of the backing
        :param size_kb: disk size in KB
        :param disk_type: disk provisioning type
        :param ds_name: datastore name where the disk is to be provisioned
        :param profileId: storage profile ID for the backing
        :param adapter_type: disk adapter type
        :return: spec for creation
        """
        create_spec = self._get_create_spec_disk_less(name, ds_name, profileId)
        create_spec.deviceChange = self._create_specs_for_disk_add(
            size_kb, disk_type, adapter_type)
        return create_spec

    def _create_backing_int(self, folder, resource_pool, host, create_spec):
        """Helper for create backing methods."""
        LOG.debug("Creating volume backing with spec: %s.", create_spec)
        task = self._session.invoke_api(self._session.vim, 'CreateVM_Task',
                                        folder, config=create_spec,
                                        pool=resource_pool, host=host)
        task_info = self._session.wait_for_task(task)
        backing = task_info.result
        LOG.info(_("Successfully created volume backing: %s."), backing)
        return backing

    def create_backing(self, name, size_kb, disk_type, folder, resource_pool,
                       host, ds_name, profileId=None, adapter_type='lsiLogic'):
        """Create backing for the volume.

        Creates a VM with one VMDK based on the given inputs.

        :param name: Name of the backing
        :param size_kb: Size in KB of the backing
        :param disk_type: VMDK type for the disk
        :param folder: Folder, where to create the backing under
        :param resource_pool: Resource pool reference
        :param host: Host reference
        :param ds_name: Datastore name where the disk is to be provisioned
        :param profileId: storage profile ID to be associated with backing
        :param adapter_type: Disk adapter type
        :return: Reference to the created backing entity
        """
        LOG.debug("Creating volume backing with name: %(name)s "
                  "disk_type: %(disk_type)s size_kb: %(size_kb)s "
                  "adapter_type: %(adapter_type)s profileId: %(profile)s at "
                  "folder: %(folder)s resource_pool: %(resource_pool)s "
                  "host: %(host)s datastore_name: %(ds_name)s.",
                  {'name': name, 'disk_type': disk_type, 'size_kb': size_kb,
                   'folder': folder, 'resource_pool': resource_pool,
                   'ds_name': ds_name, 'profile': profileId, 'host': host,
                   'adapter_type': adapter_type})

        create_spec = self.get_create_spec(name, size_kb, disk_type, ds_name,
                                           profileId, adapter_type)
        return self._create_backing_int(folder, resource_pool, host,
                                        create_spec)

    def create_backing_disk_less(self, name, folder, resource_pool,
                                 host, ds_name, profileId=None):
        """Create disk-less volume backing.

        This type of backing is useful for creating volume from image. The
        downloaded image from the image service can be copied to a virtual
        disk of desired provisioning type and added to the backing VM.

        :param name: Name of the backing
        :param folder: Folder where the backing is created
        :param resource_pool: Resource pool reference
        :param host: Host reference
        :param ds_name: Name of the datastore used for VM storage
        :param profileId: Storage profile ID to be associated with backing
        :return: Reference to the created backing entity
        """
        LOG.debug("Creating disk-less volume backing with name: %(name)s "
                  "profileId: %(profile)s at folder: %(folder)s "
                  "resource pool: %(resource_pool)s host: %(host)s "
                  "datastore_name: %(ds_name)s.",
                  {'name': name, 'profile': profileId, 'folder': folder,
                   'resource_pool': resource_pool, 'host': host,
                   'ds_name': ds_name})

        create_spec = self._get_create_spec_disk_less(name, ds_name, profileId)
        return self._create_backing_int(folder, resource_pool, host,
                                        create_spec)

    def get_datastore(self, backing):
        """Get datastore where the backing resides.

        :param backing: Reference to the backing
        :return: Datastore reference to which the backing belongs
        """
        return self._session.invoke_api(vim_util, 'get_object_property',
                                        self._session.vim, backing,
                                        'datastore').ManagedObjectReference[0]

    def get_summary(self, datastore):
        """Get datastore summary.

        :param datastore: Reference to the datastore
        :return: 'summary' property of the datastore
        """
        return self._session.invoke_api(vim_util, 'get_object_property',
                                        self._session.vim, datastore,
                                        'summary')

    def _get_relocate_spec(self, datastore, resource_pool, host,
                           disk_move_type):
        """Return spec for relocating volume backing.

        :param datastore: Reference to the datastore
        :param resource_pool: Reference to the resource pool
        :param host: Reference to the host
        :param disk_move_type: Disk move type option
        :return: Spec for relocation
        """
        cf = self._session.vim.client.factory
        relocate_spec = cf.create('ns0:VirtualMachineRelocateSpec')
        relocate_spec.datastore = datastore
        relocate_spec.pool = resource_pool
        relocate_spec.host = host
        relocate_spec.diskMoveType = disk_move_type

        LOG.debug("Spec for relocating the backing: %s." % relocate_spec)
        return relocate_spec

    def relocate_backing(self, backing, datastore, resource_pool, host):
        """Relocates backing to the input datastore and resource pool.

        The implementation uses moveAllDiskBackingsAndAllowSharing disk move
        type.

        :param backing: Reference to the backing
        :param datastore: Reference to the datastore
        :param resource_pool: Reference to the resource pool
        :param host: Reference to the host
        """
        LOG.debug("Relocating backing: %(backing)s to datastore: %(ds)s "
                  "and resource pool: %(rp)s." %
                  {'backing': backing, 'ds': datastore, 'rp': resource_pool})

        # Relocate the volume backing
        disk_move_type = 'moveAllDiskBackingsAndAllowSharing'
        relocate_spec = self._get_relocate_spec(datastore, resource_pool, host,
                                                disk_move_type)
        task = self._session.invoke_api(self._session.vim, 'RelocateVM_Task',
                                        backing, spec=relocate_spec)
        LOG.debug("Initiated relocation of volume backing: %s." % backing)
        self._session.wait_for_task(task)
        LOG.info(_("Successfully relocated volume backing: %(backing)s "
                   "to datastore: %(ds)s and resource pool: %(rp)s.") %
                 {'backing': backing, 'ds': datastore, 'rp': resource_pool})

    def move_backing_to_folder(self, backing, folder):
        """Move the volume backing to the folder.

        :param backing: Reference to the backing
        :param folder: Reference to the folder
        """
        LOG.debug("Moving backing: %(backing)s to folder: %(fol)s." %
                  {'backing': backing, 'fol': folder})
        task = self._session.invoke_api(self._session.vim,
                                        'MoveIntoFolder_Task', folder,
                                        list=[backing])
        LOG.debug("Initiated move of volume backing: %(backing)s into the "
                  "folder: %(fol)s." % {'backing': backing, 'fol': folder})
        self._session.wait_for_task(task)
        LOG.info(_("Successfully moved volume backing: %(backing)s into the "
                   "folder: %(fol)s.") % {'backing': backing, 'fol': folder})

    def create_snapshot(self, backing, name, description, quiesce=False):
        """Create snapshot of the backing with given name and description.

        :param backing: Reference to the backing entity
        :param name: Snapshot name
        :param description: Snapshot description
        :param quiesce: Whether to quiesce the backing when taking snapshot
        :return: Created snapshot entity reference
        """
        LOG.debug("Snapshoting backing: %(backing)s with name: %(name)s." %
                  {'backing': backing, 'name': name})
        task = self._session.invoke_api(self._session.vim,
                                        'CreateSnapshot_Task',
                                        backing, name=name,
                                        description=description,
                                        memory=False, quiesce=quiesce)
        LOG.debug("Initiated snapshot of volume backing: %(backing)s "
                  "named: %(name)s." % {'backing': backing, 'name': name})
        task_info = self._session.wait_for_task(task)
        snapshot = task_info.result
        LOG.info(_("Successfully created snapshot: %(snap)s for volume "
                   "backing: %(backing)s.") %
                 {'snap': snapshot, 'backing': backing})
        return snapshot

    @staticmethod
    def _get_snapshot_from_tree(name, root):
        """Get snapshot by name from the snapshot tree root.

        :param name: Snapshot name
        :param root: Current root node in the snapshot tree
        :return: None in the snapshot tree with given snapshot name
        """
        if not root:
            return None
        if root.name == name:
            return root.snapshot
        if (not hasattr(root, 'childSnapshotList') or
                not root.childSnapshotList):
            # When root does not have children, the childSnapshotList attr
            # is missing sometime. Adding an additional check.
            return None
        for node in root.childSnapshotList:
            snapshot = VMwareVolumeOps._get_snapshot_from_tree(name, node)
            if snapshot:
                return snapshot

    def get_snapshot(self, backing, name):
        """Get snapshot of the backing with given name.

        :param backing: Reference to the backing entity
        :param name: Snapshot name
        :return: Snapshot entity of the backing with given name
        """
        snapshot = self._session.invoke_api(vim_util, 'get_object_property',
                                            self._session.vim, backing,
                                            'snapshot')
        if not snapshot or not snapshot.rootSnapshotList:
            return None
        for root in snapshot.rootSnapshotList:
            return VMwareVolumeOps._get_snapshot_from_tree(name, root)

    def delete_snapshot(self, backing, name):
        """Delete a given snapshot from volume backing.

        :param backing: Reference to the backing entity
        :param name: Snapshot name
        """
        LOG.debug("Deleting the snapshot: %(name)s from backing: "
                  "%(backing)s." %
                  {'name': name, 'backing': backing})
        snapshot = self.get_snapshot(backing, name)
        if not snapshot:
            LOG.info(_("Did not find the snapshot: %(name)s for backing: "
                       "%(backing)s. Need not delete anything.") %
                     {'name': name, 'backing': backing})
            return
        task = self._session.invoke_api(self._session.vim,
                                        'RemoveSnapshot_Task',
                                        snapshot, removeChildren=False)
        LOG.debug("Initiated snapshot: %(name)s deletion for backing: "
                  "%(backing)s." %
                  {'name': name, 'backing': backing})
        self._session.wait_for_task(task)
        LOG.info(_("Successfully deleted snapshot: %(name)s of backing: "
                   "%(backing)s.") % {'backing': backing, 'name': name})

    def _get_folder(self, backing):
        """Get parent folder of the backing.

        :param backing: Reference to the backing entity
        :return: Reference to parent folder of the backing entity
        """
        return self._get_parent(backing, 'Folder')

    def _get_clone_spec(self, datastore, disk_move_type, snapshot):
        """Get the clone spec.

        :param datastore: Reference to datastore
        :param disk_move_type: Disk move type
        :param snapshot: Reference to snapshot
        :return: Clone spec
        """
        relocate_spec = self._get_relocate_spec(datastore, None, None,
                                                disk_move_type)
        cf = self._session.vim.client.factory
        clone_spec = cf.create('ns0:VirtualMachineCloneSpec')
        clone_spec.location = relocate_spec
        clone_spec.powerOn = False
        clone_spec.template = False
        clone_spec.snapshot = snapshot

        LOG.debug("Spec for cloning the backing: %s." % clone_spec)
        return clone_spec

    def clone_backing(self, name, backing, snapshot, clone_type, datastore):
        """Clone backing.

        If the clone_type is 'full', then a full clone of the source volume
        backing will be created. Else, if it is 'linked', then a linked clone
        of the source volume backing will be created.

        :param name: Name for the clone
        :param backing: Reference to the backing entity
        :param snapshot: Snapshot point from which the clone should be done
        :param clone_type: Whether a full clone or linked clone is to be made
        :param datastore: Reference to the datastore entity
        """
        LOG.debug("Creating a clone of backing: %(back)s, named: %(name)s, "
                  "clone type: %(type)s from snapshot: %(snap)s on "
                  "datastore: %(ds)s" %
                  {'back': backing, 'name': name, 'type': clone_type,
                   'snap': snapshot, 'ds': datastore})
        folder = self._get_folder(backing)
        if clone_type == LINKED_CLONE_TYPE:
            disk_move_type = 'createNewChildDiskBacking'
        else:
            disk_move_type = 'moveAllDiskBackingsAndDisallowSharing'
        clone_spec = self._get_clone_spec(datastore, disk_move_type, snapshot)
        task = self._session.invoke_api(self._session.vim, 'CloneVM_Task',
                                        backing, folder=folder, name=name,
                                        spec=clone_spec)
        LOG.debug("Initiated clone of backing: %s." % name)
        task_info = self._session.wait_for_task(task)
        new_backing = task_info.result
        LOG.info(_("Successfully created clone: %s.") % new_backing)
        return new_backing

    def delete_file(self, file_path, datacenter=None):
        """Delete file or folder on the datastore.

        :param file_path: Datastore path of the file or folder
        """
        LOG.debug("Deleting file: %(file)s under datacenter: %(dc)s." %
                  {'file': file_path, 'dc': datacenter})
        fileManager = self._session.vim.service_content.fileManager
        task = self._session.invoke_api(self._session.vim,
                                        'DeleteDatastoreFile_Task',
                                        fileManager,
                                        name=file_path,
                                        datacenter=datacenter)
        LOG.debug("Initiated deletion via task: %s." % task)
        self._session.wait_for_task(task)
        LOG.info(_("Successfully deleted file: %s.") % file_path)

    def get_path_name(self, backing):
        """Get path name of the backing.

        :param backing: Reference to the backing entity
        :return: Path name of the backing
        """
        return self._session.invoke_api(vim_util, 'get_object_property',
                                        self._session.vim, backing,
                                        'config.files').vmPathName

    def get_entity_name(self, entity):
        """Get name of the managed entity.

        :param entity: Reference to the entity
        :return: Name of the managed entity
        """
        return self._session.invoke_api(vim_util, 'get_object_property',
                                        self._session.vim, entity, 'name')

    def get_vmdk_path(self, backing):
        """Get the vmdk file name of the backing.

        The vmdk file path of the backing returned is of the form:
        "[datastore1] my_folder/my_vm.vmdk"

        :param backing: Reference to the backing
        :return: VMDK file path of the backing
        """
        hardware_devices = self._session.invoke_api(vim_util,
                                                    'get_object_property',
                                                    self._session.vim,
                                                    backing,
                                                    'config.hardware.device')
        if hardware_devices.__class__.__name__ == "ArrayOfVirtualDevice":
            hardware_devices = hardware_devices.VirtualDevice
        for device in hardware_devices:
            if device.__class__.__name__ == "VirtualDisk":
                bkng = device.backing
                if bkng.__class__.__name__ == "VirtualDiskFlatVer2BackingInfo":
                    return bkng.fileName

    def _get_virtual_disk_create_spec(self, size_in_kb, adapter_type,
                                      disk_type):
        """Return spec for file-backed virtual disk creation."""
        cf = self._session.vim.client.factory
        spec = cf.create('ns0:FileBackedVirtualDiskSpec')
        spec.capacityKb = size_in_kb
        spec.adapterType = VirtualDiskAdapterType.get_adapter_type(
            adapter_type)
        spec.diskType = VirtualDiskType.get_virtual_disk_type(disk_type)
        return spec

    def create_virtual_disk(self, dc_ref, vmdk_ds_file_path, size_in_kb,
                            adapter_type='busLogic', disk_type='preallocated'):
        """Create virtual disk with the given settings.

        :param dc_ref: datacenter reference
        :param vmdk_ds_file_path: datastore file path of the virtual disk
        :param size_in_kb: disk size in KB
        :param adapter_type: disk adapter type
        :param disk_type: vmdk type
        """
        virtual_disk_spec = self._get_virtual_disk_create_spec(size_in_kb,
                                                               adapter_type,
                                                               disk_type)
        LOG.debug("Creating virtual disk with spec: %s.", virtual_disk_spec)
        disk_manager = self._session.vim.service_content.virtualDiskManager
        task = self._session.invoke_api(self._session.vim,
                                        'CreateVirtualDisk_Task',
                                        disk_manager,
                                        name=vmdk_ds_file_path,
                                        datacenter=dc_ref,
                                        spec=virtual_disk_spec)
        LOG.debug("Task: %s created for virtual disk creation.", task)
        self._session.wait_for_task(task)
        LOG.debug("Created virtual disk with spec: %s.", virtual_disk_spec)

    def create_flat_extent_virtual_disk_descriptor(
            self, dc_ref, path, size_in_kb, adapter_type, disk_type):
        """Create descriptor for a single flat extent virtual disk.

        To create the descriptor, we create a virtual disk and delete its flat
        extent.

        :param dc_ref: reference to the datacenter
        :param path: descriptor datastore file path
        :param size_in_kb: size of the virtual disk in KB
        :param adapter_type: virtual disk adapter type
        :param disk_type: type of the virtual disk
        """
        LOG.debug("Creating descriptor: %(path)s with size (KB): %(size)s, "
                  "adapter_type: %(adapter_type)s and disk_type: "
                  "%(disk_type)s.",
                  {'path': path.get_descriptor_ds_file_path(),
                   'size': size_in_kb,
                   'adapter_type': adapter_type,
                   'disk_type': disk_type
                   })
        self.create_virtual_disk(dc_ref, path.get_descriptor_ds_file_path(),
                                 size_in_kb, adapter_type, disk_type)
        self.delete_file(path.get_flat_extent_ds_file_path(), dc_ref)
        LOG.debug("Created descriptor: %s.",
                  path.get_descriptor_ds_file_path())

    def copy_vmdk_file(self, dc_ref, src_vmdk_file_path, dest_vmdk_file_path):
        """Copy contents of the src vmdk file to dest vmdk file.

        During the copy also coalesce snapshots of src if present.
        dest_vmdk_file_path will be created if not already present.

        :param dc_ref: Reference to datacenter containing src and dest
        :param src_vmdk_file_path: Source vmdk file path
        :param dest_vmdk_file_path: Destination vmdk file path
        """
        LOG.debug('Copying disk data before snapshot of the VM')
        diskMgr = self._session.vim.service_content.virtualDiskManager
        task = self._session.invoke_api(self._session.vim,
                                        'CopyVirtualDisk_Task',
                                        diskMgr,
                                        sourceName=src_vmdk_file_path,
                                        sourceDatacenter=dc_ref,
                                        destName=dest_vmdk_file_path,
                                        destDatacenter=dc_ref,
                                        force=True)
        LOG.debug("Initiated copying disk data via task: %s." % task)
        self._session.wait_for_task(task)
        LOG.info(_("Successfully copied disk at: %(src)s to: %(dest)s.") %
                 {'src': src_vmdk_file_path, 'dest': dest_vmdk_file_path})

    def delete_vmdk_file(self, vmdk_file_path, dc_ref):
        """Delete given vmdk files.

        :param vmdk_file_path: VMDK file path to be deleted
        :param dc_ref: Reference to datacenter that contains this VMDK file
        """
        LOG.debug("Deleting vmdk file: %s." % vmdk_file_path)
        diskMgr = self._session.vim.service_content.virtualDiskManager
        task = self._session.invoke_api(self._session.vim,
                                        'DeleteVirtualDisk_Task',
                                        diskMgr,
                                        name=vmdk_file_path,
                                        datacenter=dc_ref)
        LOG.debug("Initiated deleting vmdk file via task: %s." % task)
        self._session.wait_for_task(task)
        LOG.info(_("Deleted vmdk file: %s.") % vmdk_file_path)

    def get_all_profiles(self):
        """Get all profiles defined in current VC.

        :return: PbmProfile data objects from VC
        """
        LOG.debug("Get all profiles defined in current VC.")
        pbm = self._session.pbm
        profile_manager = pbm.service_content.profileManager
        res_type = pbm.client.factory.create('ns0:PbmProfileResourceType')
        res_type.resourceType = 'STORAGE'
        profileIds = self._session.invoke_api(pbm, 'PbmQueryProfile',
                                              profile_manager,
                                              resourceType=res_type)
        LOG.debug("Got profile IDs: %s", profileIds)
        return self._session.invoke_api(pbm, 'PbmRetrieveContent',
                                        profile_manager,
                                        profileIds=profileIds)

    def retrieve_profile_id(self, profile_name):
        """Get the profile uuid from current VC for given profile name.

        :param profile_name: profile name as string
        :return: profile id as string
        """
        LOG.debug("Trying to retrieve profile id for %s", profile_name)
        for profile in self.get_all_profiles():
            if profile.name == profile_name:
                profileId = profile.profileId
                LOG.debug("Got profile id %(id)s for profile %(name)s.",
                          {'id': profileId, 'name': profile_name})
                return profileId

    def filter_matching_hubs(self, hubs, profile_id):
        """Filter and return only hubs that match given profile.

        :param hubs: PbmPlacementHub morefs candidates
        :param profile_id: profile id string
        :return: subset of hubs that match given profile_id
        """
        LOG.debug("Filtering hubs %(hubs)s that match profile "
                  "%(profile)s.", {'hubs': hubs, 'profile': profile_id})
        pbm = self._session.pbm
        placement_solver = pbm.service_content.placementSolver
        filtered_hubs = self._session.invoke_api(pbm, 'PbmQueryMatchingHub',
                                                 placement_solver,
                                                 hubsToSearch=hubs,
                                                 profile=profile_id)
        LOG.debug("Filtered hubs: %s", filtered_hubs)
        return filtered_hubs
