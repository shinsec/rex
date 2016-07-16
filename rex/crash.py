import logging

l = logging.getLogger("rex.Crash")

import os
import angr
import angrop
import tracer
import hashlib
import operator
from .trace_additions import ChallRespInfo, ZenPlugin
from rex.exploit import CannotExploit, CannotExplore, ExploitFactory, CGCExploitFactory
from rex.vulnerability import Vulnerability
from simuvex import SimMemoryError, s_options as so


class NonCrashingInput(Exception):
    pass


class Crash(object):
    '''
    Triage a crash using angr.
    '''

    def __init__(self, binary, crash=None, pov_file=None, aslr=None, constrained_addrs=None, crash_state=None,
                 prev_path=None, hooks=None, format_infos=None, rop_cache_tuple=None, use_rop=True,
                 explore_steps=0, angrop_object=None):
        '''
        :param binary: path to the binary which crashed
        :param crash: string of input which crashed the binary
        :param pov_file: CGC PoV describing a crash
        :param aslr: analyze the crash with aslr on or off
        :param constrained_addrs: list of addrs which have been constrained during exploration
        :param crash_state: an already traced crash state
        :param prev_path: path leading up to the crashing block
        :param hooks: dictionary of simprocedure hooks, addresses to simprocedures
        :param format_infos: a list of atoi FormatInfo objects that should be used when analyzing the crash
        :param rop_cache_tuple: a angrop tuple to load from
        :param use_rop: whether or not to use rop
        :param explore_steps: number of steps which have already been explored, should only set by exploration methods
        :param angrop_object: an angrop object, should only be set by exploration methods
        '''

        self.binary = binary
        self.crash  = crash
        self.pov_file = pov_file
        self.constrained_addrs = [ ] if constrained_addrs is None else constrained_addrs
        self.hooks = hooks
        self.explore_steps = explore_steps

        if self.explore_steps > 10:
            raise CannotExploit("Too many steps taken during crash exploration")

        # has the flag already been reconstrained?
        self._reconstrained_flag = False

        self.project = angr.Project(binary)

        # we search for ROP gadgets now to avoid the memory exhaustion bug in pypy
        # hash binary contents for rop cache name
        binhash = hashlib.md5(open(self.binary).read()).hexdigest()
        rop_cache_path = os.path.join("/tmp", "%s-%s-rop" % (os.path.basename(self.binary), binhash))

        if use_rop:
            if angrop_object is not None:
                self.rop = angrop_object
            else:
                self.rop = self.project.analyses.ROP()
                if rop_cache_tuple is not None:
                    l.info("loading rop gadgets from cache tuple")
                    self.rop._load_cache_tuple(rop_cache_tuple)
                elif os.path.exists(rop_cache_path):
                    l.info("loading rop gadgets from cache '%s'", rop_cache_path)
                    self.rop.load_gadgets(rop_cache_path)
                else:
                    self.rop.find_gadgets()
                    self.rop.save_gadgets(rop_cache_path)
        else:
            self.rop = None

        self.os = self.project.loader.main_bin.os

        # determine the aslr of a given os and arch
        if aslr is None:
            if self.os == "cgc": # cgc has no ASLR, but we don't assume a stackbase
                self.aslr = False
            else: # we assume linux is going to enfore stackbased aslr
                self.aslr = True
        else:
            self.aslr = aslr

        if crash_state is None:
            # run the tracer, grabbing the crash state
            remove_options = {so.TRACK_REGISTER_ACTIONS, so.TRACK_TMP_ACTIONS, so.TRACK_JMP_ACTIONS,
                              so.ACTION_DEPS, so.TRACK_CONSTRAINT_ACTIONS}
            add_options = {so.MEMORY_SYMBOLIC_BYTES_MAP, so.TRACK_ACTION_HISTORY, so.CONCRETIZE_SYMBOLIC_WRITE_SIZES,
                           so.CONCRETIZE_SYMBOLIC_FILE_READ_SIZES}

            # faster place to check for non-crashing inputs

            # optimized crash check
            if self.project.loader.main_bin.os == 'cgc':

                if not tracer.Runner(binary, input=self.crash).crash_mode:
                    l.warning("input did not cause a crash")
                    raise NonCrashingInput

            self._tracer = tracer.Tracer(binary, input=self.crash, pov_file=self.pov_file, resiliency=False,
                                         hooks=self.hooks, add_options=add_options, remove_options=remove_options)
            ChallRespInfo.prep_tracer(self._tracer, format_infos)
            ZenPlugin.prep_tracer(self._tracer)
            prev, crash_state = self._tracer.run(constrained_addrs)

            if crash_state is None:
                l.warning("input did not cause a crash")
                raise NonCrashingInput

            l.debug("done tracing input")
            # a path leading up to the crashing basic block
            self.prev   = prev

            # the state at crash time
            self.state  = crash_state
        else:
            self.state = crash_state
            self.prev = prev_path
            self._tracer = None

        # list of actions added during exploitation, probably better object for this attribute to belong to
        self.added_actions = [ ]

        # hacky trick to get all bytes
        #memory_writes = [ ]
        #for var in self.state.memory.mem._name_mapping.keys():
        #    memory_writes.extend(self.state.memory.addrs_for_name(var))

        memory_writes = sorted(self.state.memory.mem.get_symbolic_addrs())
        l.debug("filtering writes")
        memory_writes = [m for m in memory_writes if m/0x1000 != 0x4347c]
        user_writes = [m for m in memory_writes if any("stdin" in v for v in self.state.memory.load(m, 1).variables)]
        flag_writes = [m for m in memory_writes if any(v.startswith("cgc-flag") for v in self.state.memory.load(m, 1).variables)]
        l.debug("done filtering writes")

        self.symbolic_mem = self._segment(user_writes)
        self.flag_mem = self._segment(flag_writes)

        # crash type
        self.crash_type = None
        # action (in case of a bad write or read) which caused the crash
        self.violating_action = None

        l.debug("triaging crash")
        self._triage_crash()

### EXPOSED

    def exploitable(self):
        '''
        determine if the crash is exploitable
        :return: True if the crash's type is generally considered exploitable, False otherwise
        '''

        exploitables = [Vulnerability.IP_OVERWRITE, Vulnerability.PARTIAL_IP_OVERWRITE, Vulnerability.BP_OVERWRITE,
                Vulnerability.PARTIAL_BP_OVERWRITE, Vulnerability.WRITE_WHAT_WHERE, Vulnerability.WRITE_X_WHERE]

        return self.crash_type in exploitables

    def explorable(self):
        '''
        determine if the crash can be explored with the 'crash explorer'.
        :return: True if the crash's type lends itself to exploring, only 'arbitrary-read' for now
        '''

        return self.crash_type in [Vulnerability.ARBITRARY_READ, Vulnerability.WRITE_WHAT_WHERE, Vulnerability.WRITE_X_WHERE]

    def _prepare_exploit_factory(self, blacklist_symbolic_explore=True, **kwargs):
        # crash should have been classified at this point
        if not self.exploitable():
            raise CannotExploit("non-exploitable crash")

        if blacklist_symbolic_explore:
            if "blacklist_techniques" in kwargs:
                kwargs["blacklist_techniques"].add("explore_for_exploit")
            else:
                kwargs["blacklist_techniques"] = {"explore_for_exploit"}

        if self.os == 'cgc':
            exploit = CGCExploitFactory(self, **kwargs)
        else:
            exploit = ExploitFactory(self, **kwargs)

        return exploit

    def exploit(self, blacklist_symbolic_explore=True, **kwargs):
        '''
        craft an exploit for a crash
        '''

        factory = self._prepare_exploit_factory(blacklist_symbolic_explore, **kwargs)

        factory.initialize()
        return factory

    def yield_exploits(self, blacklist_symbolic_explore=True, **kwargs):
        '''
        craft an exploit for a crash
        '''

        factory = self._prepare_exploit_factory(blacklist_symbolic_explore, **kwargs)

        for exploit in factory.yield_exploits():
            yield exploit

    def explore(self, path_file=None):
        '''
        explore a crash further to find new bugs
        '''

        # crash should be classified at this point
        if not self.explorable():
                raise CannotExplore("non-explorable crash")

        self._reconstrain_flag_data()

        assert self.violating_action is not None

        if self.crash_type in [Vulnerability.ARBITRARY_READ]:
            self._explore_arbitrary_read(path_file)
        elif self.crash_type in [Vulnerability.WRITE_WHAT_WHERE, Vulnerability.WRITE_X_WHERE]:
            self._explore_arbitrary_write(path_file)
        else:
            raise ValueError("unknown explorable crash type: %s", self.crash_type)

    def point_to_flag(self, path_file=None):
        '''
        Create a testcase which points an arbitrary-read crash at the flag page.

        :param path_file: file to dump testcase to
        '''


        if not self.crash_type in [Vulnerability.ARBITRARY_READ]:
            raise CannotExploit("only arbitrary-reads can be exploited this way")

        self._reconstrain_flag_data()

        cp = self._get_state_pointing_to_flag(self.state, self.violating_action.addr)
        new_input = cp.posix.dumps(0)

        if path_file is not None:
            with open(path_file, 'w') as f:
                f.write(new_input)

        return new_input

    @staticmethod
    def _get_state_pointing_to_flag(state, violating_addr):
        # see if we can point at flag
        cgc_magic_page_addr = 0x4347c000
        if state.se.satisfiable(extra_constraints=
                                (violating_addr >= cgc_magic_page_addr,
                                 violating_addr < cgc_magic_page_addr+0x1000-4)):
            cp = state.copy()
            cp.add_constraints(violating_addr >= cgc_magic_page_addr)
            cp.add_constraints(violating_addr < cgc_magic_page_addr+0x1000-4)
            return cp
        else:
            raise CannotExploit("unable to point arbitrary-read at the flag page")



    def _explore_arbitrary_read(self, path_file=None):
        # crash type was an arbitrary-read, let's point the violating address at a
        # symbolic memory region

        largest_regions = sorted(self.symbolic_mem.items(),
                key=operator.itemgetter(1),
                reverse=True)

        min_read = self.state.se.min(self.violating_action.addr)
        max_read = self.state.se.max(self.violating_action.addr)

        largest_regions = map(operator.itemgetter(0), largest_regions)
        # filter addresses which fit between the min and max possible address
        largest_regions = filter(lambda x: (min_read <= x) and (x <= max_read), largest_regions)

        # populate the rest of the list with addresses from the binary
        min_addr = self.project.loader.main_bin.get_min_addr()
        max_addr = self.project.loader.main_bin.get_max_addr()
        pages = range(min_addr, max_addr, 0x1000)
        pages = filter(lambda x: (min_read <= x) and (x <= max_read), pages)

        read_addr = None
        constraint = None
        for addr in largest_regions + pages:
            read_addr = addr
            constraint = self.violating_action.addr == addr

            if self.state.se.satisfiable(extra_constraints=(constraint,)):
                break

            constraint = None

        if constraint is None:
            raise CannotExploit("unable to find suitable read address, cannot explore")

        self.state.add_constraints(constraint)

        l.debug("constraining input to read from address %#x", read_addr)

        l.info("starting a new crash exploration phase based off the crash at address 0x%x", self.violating_action.ins_addr)

        new_input = self.state.posix.dumps(0)
        if path_file is not None:
            l.info("dumping new crash evading input into file '%s'", path_file)
            with open(path_file, 'w') as f:
                f.write(new_input)

        # create a new crash object starting here
        use_rop = False if self.rop is None else True
        self.__init__(self.binary,
                new_input,
                explore_steps=self.explore_steps + 1,
                constrained_addrs=self.constrained_addrs + [self.violating_action],
                use_rop=use_rop,
                angrop_object=self.rop)

    def _explore_arbitrary_write(self, path_file=None):
        # crash type was an arbitrary-write, this routine doesn't care about taking advantage
        # of the write it just wants to try to find a more valuable crash by pointing the write
        # at some writable memory

        # find a writable data segment

        elf_objects = self.project.loader.all_elf_objects

        assert len(elf_objects) > 0, "target binary is not ELF or CGC, unsupported by rex"

        min_write = self.state.se.min(self.violating_action.addr)
        max_write = self.state.se.max(self.violating_action.addr)

        segs = [ ]
        for eobj in elf_objects:
            segs.extend(filter(lambda s: s.is_writable, eobj.segments))

        segs = filter(lambda s: (s.min_addr <= max_write) and (s.max_addr >= min_write), segs)

        write_addr = None
        constraint = None
        for seg in segs:
            for page in range(seg.min_addr, seg.max_addr, 0x1000):
                write_addr = page
                constraint = self.violating_action.addr == page

                if self.state.se.satisfiable(extra_constraints=(constraint,)):
                    break

                constraint = None

        if constraint is None:
            raise CannotExploit("Cannot point write at any writeable segments")

        self.state.add_constraints(constraint)
        l.debug("constraining input to write to address %#x", write_addr)

        l.info("starting a new crash exploration phase based off the crash at address %#x",
                self.violating_action.ins_addr)

        new_input = self.state.posix.dumps(0)
        if path_file is not None:
            l.info("dumping new crash evading input into file '%s'", path_file)
            with open(path_file, 'w') as f:
                f.write(new_input)

        use_rop = False if self.rop is None else True
        self.__init__(self.binary,
                new_input,
                explore_steps=self.explore_steps + 1,
                constrained_addrs=self.constrained_addrs + [self.violating_action],
                use_rop=use_rop,
                angrop_object=self.rop)

    def copy(self):
        cp = Crash.__new__(Crash)
        cp.binary = self.binary
        cp.crash = self.crash
        cp.project = self.project
        cp.os = self.os
        cp.aslr = self.aslr
        cp.prev = self.prev.copy()
        cp.state = self.state.copy()
        cp.rop = self.rop
        cp.added_actions = list(self.added_actions)
        cp.symbolic_mem = self.symbolic_mem.copy()
        cp.crash_type = self.crash_type
        cp._tracer = self._tracer
        cp.violating_action = self.violating_action
        cp.explore_steps = self.explore_steps
        cp.constrained_addrs = list(self.constrained_addrs)

        return cp

### UTIL

    def _reconstrain_flag_data(self):

        if not self._reconstrained_flag:
            l.info("reconstraining flag")

            replace_dict = dict()
            for c in self._tracer.preconstraints:
                if any([v.startswith('cgc-flag') for v in list(c.variables)]):
                    concrete = next(a for a in c.args if not a.symbolic)
                    symbolic = next(a for a in c.args if a.symbolic)
                    replace_dict[symbolic.cache_key] = concrete
            cons = self.state.se.constraints
            new_cons = []
            for c in cons:
                new_c = c.replace_dict(replace_dict)
                new_cons.append(new_c)
            self.state.release_plugin("solver_engine")
            self.state.add_constraints(*new_cons)
            self.state.downsize()
            self.state.se.simplify()
            self._reconstrained_flag = True

    @staticmethod
    def _segment(memory_writes):
        segments = { }
        memory_writes = sorted(memory_writes)

        if len(memory_writes) == 0:
            return segments

        current_w_start = memory_writes[0]
        current_w_end = current_w_start + 1

        for write in memory_writes[1:]:
            write_start = write
            write_len = 1

            # segment is completely seperate
            if write_start > current_w_end:
                # store the old segment
                segments[current_w_start] = current_w_end - current_w_start

                # new segment, update start and end
                current_w_start = write_start
                current_w_end = write_start + write_len
            else:
                # update the end of the current segment, the segment `write` exists within current
                current_w_end = max(current_w_end, write_start + write_len)


        # write in the last segment
        segments[current_w_start] = current_w_end - current_w_start

        return segments

    def _symbolic_control(self, st):
        '''
        determine the amount of symbolic bits in an ast, useful to determining how much control we have
        over registers
        '''

        sbits = 0

        for bitidx in xrange(self.state.arch.bits):
            if st[bitidx].symbolic:
                sbits += 1

        return sbits

    def _triage_crash(self):
        ip = self.state.regs.ip
        bp = self.state.regs.bp

        # we assume a symbolic eip is always exploitable
        if self.state.se.symbolic(ip):
            # how much control of ip do we have?
            if self._symbolic_control(ip) >= self.state.arch.bits:
                l.info("detected ip overwrite vulnerability")
                self.crash_type = Vulnerability.IP_OVERWRITE
            else:
                l.info("detected partial ip overwrite vulnerability")
                self.crash_type = Vulnerability.PARTIAL_IP_OVERWRITE

            return

        if self.state.se.symbolic(bp):
            # how much control of bp do we have
            if self._symbolic_control(bp) >= self.state.arch.bits:
                l.info("detected bp overwrite vulnerability")
                self.crash_type = Vulnerability.BP_OVERWRITE
            else:
                l.info("detected partial bp overwrite vulnerability")
                self.crash_type = Vulnerability.PARTIAL_BP_OVERWRITE

            return

        # if nothing obvious is symbolic let's look at actions

        # grab the all actions in the last basic block
        symbolic_actions = [ ]
        for a in list(self.prev.state.log.actions) + list(self.state.log.actions):
            if a.type == 'mem':
                if self.state.se.symbolic(a.addr):
                    symbolic_actions.append(a)

        # TODO: pick the crashing action based off the crashing instruction address,
        # crash fixup attempts will break on this
        for sym_action in symbolic_actions:
            if sym_action.action == "write":
                if self.state.se.symbolic(sym_action.data):
                    l.info("detected write-what-where vulnerability")
                    self.crash_type = Vulnerability.WRITE_WHAT_WHERE
                else:
                    l.info("detected write-x-where vulnerability")
                    self.crash_type = Vulnerability.WRITE_X_WHERE

                self.violating_action = sym_action
                break

            if sym_action.action == "read":
                # special vulnerability type, if this is detected we can explore the crash further
                l.info("detected arbitrary-read vulnerability")
                self.crash_type = Vulnerability.ARBITRARY_READ

                self.violating_action = sym_action
                break

        return

### CLASS METHODS
    @classmethod
    def quick_triage(cls, binary, crash):
        """
        Quickly triage a crash with just QEMU. Less accurate, but much faster.
        :param binary: path to binary which crashed
        :param crash: input which caused crash
        :return: a vulnerability classification and the value of eip where the crash occured
        """

        l.debug("quick triaging crash against '%s'", binary)
        r = tracer.Runner(binary, crash)
        if not r.crash_mode:
            raise NonCrashingInput("input did not cause a crash")

        if r.os != "cgc":
            raise ValueError("quick_triage is only available for CGC binaries")

        project = angr.Project(binary)
        # triage the crash based of the register values and memory at crashtime
        # look for the most valuable crashes first

        pc = r.reg_vals['eip']
        l.debug('crash occured at %#x', pc)
        l.debug("checking if ip is null")
        if pc < 0x1000:
            return pc, Vulnerability.NULL_DEREFERENCE

        l.debug("checking if ip register points to executable memory")
        # was ip mapped?
        ip_overwritten = False
        try:
            perms = r.memory.permissions(pc)
            # check if the execute bit is marked, this is an AST
            l.debug("ip points to mapped memory")
            if not perms.symbolic and not ((perms & 4) == 4).args[0]:
                l.debug("ip appears to be uncontrolled")
                return pc, Vulnerability.UNCONTROLLED_IP_OVERWRITE

        except SimMemoryError:
            ip_overwritten = True

        if ip_overwritten:
            # let's see if we can classify it as a partial overwrite
            # this is done by seeing if the most signifigant bytes of
            # pc could be a mapping
            cgc_object = project.loader.all_elf_objects[0]
            base = cgc_object.get_min_addr() & 0xff000000
            while base < cgc_object.get_max_addr():
                if pc & 0xff000000 == base:
                    l.debug("ip appears to only be partially controlled")
                    return pc, Vulnerability.PARTIAL_IP_OVERWRITE
                base += 0x01000000

            l.debug("ip appears to be completely controlled")
            return pc, Vulnerability.IP_OVERWRITE

        l.debug("checking if a read or write caused the crash")
        # wasn't an ip overwrite, check reads and writes
        start_state = project.factory.entry_state(addr=pc)

        # set registers
        start_state.regs.eax = r.reg_vals['eax']
        start_state.regs.ebx = r.reg_vals['ebx']
        start_state.regs.ecx = r.reg_vals['ecx']
        start_state.regs.edx = r.reg_vals['edx']
        start_state.regs.esi = r.reg_vals['esi']
        start_state.regs.edi = r.reg_vals['edi']
        start_state.regs.esp = r.reg_vals['esp']
        start_state.regs.ebp = r.reg_vals['ebp']

        pth = project.factory.path(start_state)
        next_pth = pth.step(num_inst=1)[0]

        posit = None
        for a in next_pth.actions:
            if a.type == 'mem':

                target_addr = start_state.se.any_int(a.addr)
                if target_addr < 0x1000:
                    l.debug("attempt to write or read to address of NULL")
                    return pc, Vulnerability.NULL_DEREFERENCE

                # we will take the last memory action, so things like an `add` instruction
                # are triaged as a 'write' opposed to a 'read'
                if a.action == 'write':
                    l.debug("write detected")
                    posit = Vulnerability.WRITE_WHAT_WHERE
                    # if it's trying to write to a non-writeable address which is mapped
                    # it's most likely uncontrolled
                    if target_addr & 0xfff00000 == 0:
                        l.debug("write attempt at a suspiciously small address, assuming uncontrolled")
                        return pc, Vulnerability.UNCONTROLLED_WRITE

                    try:
                        perms = r.memory.permissions(target_addr)
                        if not perms.symbolic and not ((perms & 2) == 2).args[0]:
                            l.debug("write attempt at a read-only page, assuming uncontrolled")
                            return pc, Vulnerability.UNCONTROLLED_WRITE

                    except SimMemoryError:
                        pass

                elif a.action == 'read':
                    l.debug("read detected")
                    posit = Vulnerability.ARBITRARY_READ
                else:
                    # sanity checking
                    raise ValueError("unrecognized memory action encountered %s" % a.action)

        if posit is None:
            l.debug("crash was not able to be triaged")
            posit = 'unknown'

        # returning 'unknown' if crash does not fall into one of our obvious categories
        return pc, posit
