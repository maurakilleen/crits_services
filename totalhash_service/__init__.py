# Copyright (c) 2014, Adam Polkosnik, Team Cymru.  All rights reserved.
# Copyright (c) 2014, The MITRE Corporation. All rights reserved.

# Source code distributed pursuant to license agreement.
# PEhash computing code is from Team Cymru.
# Wrapping into the CRITs module done by Adam Polkosnik.
# The Totalhash API lookup done by Wesley Shields.

from __future__ import division

import pefile
import bitstring
import string
import bz2
import hashlib
import logging
import urllib
import urllib2
import hmac

from django.conf import settings

from crits.services.core import Service, ServiceConfigOption

logger = logging.getLogger(__name__)


class TotalHashService(Service):
    """
    (PE Clustering) as implemented by Team Cymru' PEhash http://totalhash.com/pehash-source-code/.

    Optionally look up the resulting hash on totalhash.
    """

    name = "totalhash"
    version = '0.1.0'
    type_ = Service.TYPE_CUSTOM
    supported_types = ['Sample']
    default_config = [
        ServiceConfigOption('th_api_key',
                            ServiceConfigOption.STRING,
                            description="Required. Obtain from Totalhash.",
                            required=True,
                            private=True),
        ServiceConfigOption('th_user',
                            ServiceConfigOption.STRING,
                            description="Required. Obtain from Totalhash.",
                            required=True,
                            private=True),
        ServiceConfigOption('th_query_url',
                            ServiceConfigOption.STRING,
                            default='https://api.totalhash.com/search',
                            required=True,
                            private=True),
    ]

    @staticmethod
    def valid_for(context):
        # Only run on PE files
        return context.is_pe()

    def _scan(self, context):
        try:
            pe = pefile.PE(data=context.data)
        except pefile.PEFormatError as e:
            self._error("A PEFormatError occurred: %s" % e)
            return
        self._get_pehash(pe)

        # If we have an API key, go ahead and look it up.
        key = str(self.config.get('th_api_key', ''))
        user = self.config.get('th_user', '')
        url = self.config.get('th_query_url', '')

        # XXX: Context doesn't provide sha1. When we move away from contexts
        # this can just use str(obj.sha1)
        h = hashlib.sha1(context.data).hexdigest()

        if not key:
            self._add_result('Analysis Link', url + "/analysis/" + h)
            self._info("No API key, not checking Totalhash.")
            return

        signature = hmac.new(key, msg=h, digestmod=hashlib.sha256).hexdigest()
        params = "/analysis/" + h + "&id=" + user + "&sign=" + signature
        req = urllib2.Request(url + params)

        if settings.HTTP_PROXY:
            proxy = urllib2.ProxyHandler({'https': settings.HTTP_PROXY})
            opener = urllib2.build_opener(proxy)
            urllib2.install_opener(opener)

        try:
            response = urllib2.urlopen(req)
            data = response.read()
        except Exception as e:
            logger.info("Totalhash: network connection error (%s)" % e)
            self._info("Network connection error checking totalhash (%s)" % e)
            return

        from lxml import etree
        try:
            root = etree.fromstring(data)
        except Exception as e:
            logger.error("Totalhash: parse error (%s)" % e)
            self._error("Error parsing results: %s" % e)
            return

        self._add_result('Analysis Metadata', root.attrib['time'])

        it = root.getiterator('av')
        for av in it:
            stats = {
                'scanner': av.attrib['scanner'],
                'timestamp': av.attrib['timestamp']
            }
            self._add_result('AV', av.attrib['signature'], stats)

        it = root.getiterator('process')
        for proc in it:
            filename = proc.attrib['filename']
            # Some entries appear with an empty filename and nothing else.
            if filename == '':
                continue
            pid = proc.attrib['pid']

            dlls = []
            for dll in proc.findall('dll_handling_section/load_dll'):
                dlls.append(dll.attrib['filename'])

            files = []
            for file_ in proc.findall('filesystem_section/create_file'):
                info = {
                    'Filename': file_.attrib['srcfile'],
                    'Action': 'create'
                }
                files.append(info)
            for file_ in proc.findall('filesystem_section/delete_file'):
                info = {
                    'Filename': file_.attrib['srcfile'],
                    'Action': 'delete'
                }
                files.append(info)

            procs = []
            for cp in proc.findall('process_section/create_process'):
                info = {
                    'Cmdline': cp.attrib['cmdline'],
                    'Target PID': cp.attrib['targetpid'],
                    'Action': 'create'
                }
                procs.append(info)
            for op in proc.findall('process_section/open_process'):
                info = {
                    'Target PID': op.attrib['targetpid'],
                    'API': op.attrib['apifunction'],
                    'Action': 'open'
                }
                procs.append(info)

            hosts = []
            for host in proc.findall('winsock_section/getaddrinfo'):
                hosts.append(host.attrib['requested_host'])

            mutexes = []
            for mutex in proc.findall('mutex_section/create_mutex'):
                mutexes.append(mutex.attrib['name'])

            hooks = []
            for hook in proc.findall('windows_hook_section/set_windows_hook'):
                hooks.append(hook.attrib['hookid'])

            regs = []
            for reg in proc.findall('registry_section/set_value'):
                info = {
                    'Key': reg.attrib['key'],
                    'Value': reg.attrib['value'],
                    'Action': 'set'
                }
                regs.append(info)

            svcs = []
            for svc in proc.findall('service_section/create_service'):
                info = {
                    'Display Name': svc.attrib['displayname'],
                    'Image Path': svc.attrib['imagepath'],
                    'Action': 'create'
                }
                svcs.append(info)
            for svc in proc.findall('service_section/start_service'):
                info = {
                    'Display Name': svc.attrib['displayname'],
                    'Action': 'start'
                }
                svcs.append(info)

            sysinfo = []
            for si in proc.findall('system_info_section/check_for_debugger'):
                sysinfo.append(si.attrib['apifunction'])

            stats = {
                'PID': pid,
                'Loaded DLLs': ', '.join([dll for dll in dlls]),
                'Files': files,
                'Processes': procs,
                'Requested hosts': ', '.join([host for host in hosts]),
                'Created mutexes': ', '.join([mutex for mutex in mutexes]),
                'Registry keys': regs,
                'Created services': svcs,
                'Hooks': ', '.join([hook for hook in hooks]),
                'System checks': ', '.join([si for si in sysinfo])
            }
            self._add_result('Processes', filename, stats)

        it = root.getiterator('running_process')
        for proc in it:
            stats = {
                'PID': proc.attrib['pid'],
                'PPID': proc.attrib['ppid']
            }
            self._add_result('Running processes', proc.attrib['filename'], stats)

        it = root.getiterator('flows')
        for flow in it:
            info =  {
                'Source IP': flow.attrib['src_ip'],
                'Source Port': flow.attrib['src_port'],
                'Dest Port': flow.attrib['dst_port'],
                'Bytes': flow.attrib['bytes']
            }

            if flow.attrib['protocol'] == '6':
                proto = 'TCP'
            elif flow.attrib['protocol'] == '17':
                proto = 'UDP'
            else:
                proto = flow.attrib['protocol']

            info['Protocol'] = proto

            self._add_result('Flows', flow.attrib['dst_ip'], info)

        it = root.getiterator('dns')
        for dns in it:
            info = {
                'Type': dns.attrib['type'],
                'IP': dns.attrib.get('ip', 'Not resolved.')
            }
            self._add_result('DNS', dns.attrib['rr'], info)
        it = root.getiterator('http')
        for http in it:
            info =  {
                'User Agent': http.attrib['user_agent'],
                'Type': http.attrib['type']
            }

            self._add_result('HTTP', http.text, info)

    def _get_pehash(self, exe):
        #image characteristics
        img_chars = bitstring.BitArray(hex(exe.FILE_HEADER.Characteristics))
        #pad to 16 bits
        img_chars = bitstring.BitArray(bytes=img_chars.tobytes())
        img_chars_xor = img_chars[0:7] ^ img_chars[8:15]

        #start to build pehash
        pehash_bin = bitstring.BitArray(img_chars_xor)

        #subsystem - 
        sub_chars = bitstring.BitArray(hex(exe.FILE_HEADER.Machine))
        #pad to 16 bits
        sub_chars = bitstring.BitArray(bytes=sub_chars.tobytes())
        sub_chars_xor = sub_chars[0:7] ^ sub_chars[8:15]
        pehash_bin.append(sub_chars_xor)

        #Stack Commit Size
        stk_size = bitstring.BitArray(hex(exe.OPTIONAL_HEADER.SizeOfStackCommit))
        stk_size_bits = string.zfill(stk_size.bin, 32)
        #now xor the bits
        stk_size = bitstring.BitArray(bin=stk_size_bits)
        stk_size_xor = stk_size[8:15] ^ stk_size[16:23] ^ stk_size[24:31]
        #pad to 8 bits
        stk_size_xor = bitstring.BitArray(bytes=stk_size_xor.tobytes())
        pehash_bin.append(stk_size_xor)

        #Heap Commit Size
        hp_size = bitstring.BitArray(hex(exe.OPTIONAL_HEADER.SizeOfHeapCommit))
        hp_size_bits = string.zfill(hp_size.bin, 32)
        #now xor the bits
        hp_size = bitstring.BitArray(bin=hp_size_bits)
        hp_size_xor = hp_size[8:15] ^ hp_size[16:23] ^ hp_size[24:31]
        #pad to 8 bits
        hp_size_xor = bitstring.BitArray(bytes=hp_size_xor.tobytes())
        pehash_bin.append(hp_size_xor)

        #Section chars
        for section in exe.sections:
            #virutal address
            sect_va =  bitstring.BitArray(hex(section.VirtualAddress))
            sect_va = bitstring.BitArray(bytes=sect_va.tobytes())
            pehash_bin.append(sect_va)    

            #rawsize
            sect_rs =  bitstring.BitArray(hex(section.SizeOfRawData))
            sect_rs = bitstring.BitArray(bytes=sect_rs.tobytes())
            sect_rs_bits = string.zfill(sect_rs.bin, 32)
            sect_rs = bitstring.BitArray(bin=sect_rs_bits)
            sect_rs = bitstring.BitArray(bytes=sect_rs.tobytes())
            sect_rs_bits = sect_rs[8:31]
            pehash_bin.append(sect_rs_bits)

            #section chars
            sect_chars =  bitstring.BitArray(hex(section.Characteristics))
            sect_chars = bitstring.BitArray(bytes=sect_chars.tobytes())
            sect_chars_xor = sect_chars[16:23] ^ sect_chars[24:31]
            pehash_bin.append(sect_chars_xor)

            #entropy calulation
            address = section.VirtualAddress
            size = section.SizeOfRawData
            raw = exe.write()[address+size:]
            if size == 0: 
                kolmog = bitstring.BitArray(float=1, length=32)
                pehash_bin.append(kolmog[0:7])
                continue
            bz2_raw = bz2.compress(raw)
            bz2_size = len(bz2_raw)
            #k = round(bz2_size / size, 5)
            k = bz2_size / size
            kolmog = bitstring.BitArray(float=k, length=32)
            pehash_bin.append(kolmog[0:7])

        m = hashlib.sha1()
        m.update(pehash_bin.tobytes())
        output = m.hexdigest()
        self._add_result('PEhash value', "%s" % output, {'Value': output})

