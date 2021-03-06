from simuvex import SimStatePlugin
import simuvex
import claripy
from simuvex import SimMemoryError

import string
import logging
l = logging.getLogger("rex.trace_additions")
l.setLevel("DEBUG")


"""
This file contains objects to track additional information during a trace or
modify symbolic variables during a trace.

The ChallRespInfo plugin tracks variables in stdin and stdout to enable handling of challenge response
It handles atoi/int2str in a special manner since path constraints will usually prevent
their values from being modified

The Zen plugin simplifies expressions created from variables in the flag page (losing some accuracy)
to avoid situations where they become to complex for z3, but the actual equation doesn't matter much.
This can happen in challenge response if all of the values in the flag page are multiplied together
before being printed.
"""


class FormatInfo(object):
    def copy(self):
        raise NotImplementedError

    def compute(self, state):
        raise NotImplementedError

    def get_type(self):
        raise NotImplementedError


class FormatInfoStrToInt(FormatInfo):
    def __init__(self, addr, func_name, str_arg_num, base, base_arg, allows_negative):
        # the address of the function
        self.addr = addr
        # the name of the function
        self.func_name = func_name
        # the argument which is a string
        self.str_arg_num = str_arg_num
        # the base of the string
        self.base = base
        # the argument which represents the base
        self.base_arg = base_arg
        # whether or not negatives can be passed
        self.allows_negative = allows_negative
        # the input_val (computed at the start of function call)
        self.input_val = None
        self.input_base = None

    def copy(self):
        out = FormatInfoStrToInt(self.addr, self.func_name, self.str_arg_num,
                                 self.base, self.base_arg, self.allows_negative)
        return out

    def compute(self, state):
        self.input_val = simuvex.s_cc.SimCCCdecl(state.arch).arg(state, self.str_arg_num)
        if self.base_arg is not None:
            self.input_base = state.se.any_int(simuvex.s_cc.SimCCCdecl(state.arch).arg(state, self.base_arg))
            if self.input_base == 0:
                self.input_base = 16
        else:
            self.input_base = self.base

    def get_type(self):
        return "StrToInt"

class FormatInfoIntToStr(FormatInfo):
    def __init__(self, addr, func_name, int_arg_num, str_dst_num, base, base_arg):
        # the address of the function
        self.addr = addr
        # the name of the function
        self.func_name = func_name
        # the argument which is a string
        self.int_arg_num = int_arg_num
        # the argument which is the dest buf
        self.str_dst_num = str_dst_num
        # the base of the string
        self.base = base
        # the argument which represents the base
        self.base_arg = base_arg
        # the input_val and str addr (computed at the start of function call)
        self.input_val = None
        self.input_base = None
        self.str_dst_addr = None

    def copy(self):
        out = FormatInfoIntToStr(self.addr, self.func_name, self.int_arg_num,
                                 self.str_dst_num, self.base, self.base_arg)
        return out

    def compute(self, state):
        self.input_val = simuvex.s_cc.SimCCCdecl(state.arch).arg(state, self.int_arg_num)
        if self.base_arg is not None:
            self.input_base = state.se.any_int(simuvex.s_cc.SimCCCdecl(state.arch).arg(state, self.base_arg))
            if self.input_base == 0:
                self.input_base = 16
        else:
            self.input_base = self.base
        self.str_dst_addr = simuvex.s_cc.SimCCCdecl(state.arch).arg(state, self.str_dst_num)

    def get_type(self):
        return "IntToStr"


class FormatInfoDontConstrain(FormatInfo):
    def __init__(self, addr, func_name, check_symbolic_arg):
        self.addr = addr
        self.func_name = func_name
        self.check_symbolic_arg = check_symbolic_arg

    def copy(self):
        out = FormatInfoDontConstrain(self.addr, self.func_name, self.check_symbolic_arg)
        return out

    def compute(self, state):
        pass

    def get_type(self):
        return "DontConstrain"


def int2base(x, base):
    digs = string.digits + string.letters
    if x < 0:
        sign = -1
    elif x == 0:
        return digs[0]
    else:
        sign = 1
    x *= sign
    digits = []
    while x:
        digits.append(digs[x % base])
        x /= base
    if sign < 0:
        digits.append('-')
    digits.reverse()
    return ''.join(digits)


def generic_info_hook(state):
    addr = state.se.any_int(state.regs.ip)
    chall_resp_plugin = state.get_plugin("chall_resp_info")

    format_info = chall_resp_plugin.format_infos[addr].copy()
    if format_info.get_type() == "DontConstrain":
        arg_num = format_info.check_symbolic_arg
        arg = simuvex.s_cc.SimCCCdecl(state.arch).arg(state, arg_num)
        if state.mem[arg].string.resolved.symbolic:
            l.warning("symbolic arg not hooking")
            return

    # remove a current pending info
    if chall_resp_plugin.pending_info is not None:
        chall_resp_plugin.backup_pending_info.append((chall_resp_plugin.ret_addr_to_unhook,
                                                      chall_resp_plugin.pending_info))
        # undo the stops
        chall_resp_plugin.project.unhook(chall_resp_plugin.ret_addr_to_unhook)
        chall_resp_plugin.ret_addr_to_unhook = None
        chall_resp_plugin.pending_info = None

    # hook the return address
    ret_addr = state.se.any_int(state.memory.load(state.regs.sp, 4, endness="Iend_LE"))
    chall_resp_plugin.ret_addr_to_unhook = ret_addr
    chall_resp_plugin.project.hook(ret_addr, end_info_hook, length=0)

    format_info.compute(state)

    chall_resp_plugin.pending_info = format_info
    l.debug("starting hook for %s at %#x", format_info.func_name, format_info.addr)


def end_info_hook(state):
    chall_resp_plugin = state.get_plugin("chall_resp_info")
    pending_info = chall_resp_plugin.pending_info

    # undo the stops
    chall_resp_plugin.project.unhook(chall_resp_plugin.ret_addr_to_unhook)
    chall_resp_plugin.ret_addr_to_unhook = None
    chall_resp_plugin.pending_info = None

    # replace the result with a symbolic variable
    # also add a constraint that points out what the input is
    if pending_info.get_type() == "StrToInt":
        # mark the input
        input_val = state.mem[pending_info.input_val].string.resolved
        result = state.se.BVV(state.se.any_str(state.regs.eax))
        real_len = chall_resp_plugin.get_real_len(input_val, pending_info.input_base,
                                                  result, pending_info.allows_negative)

        if real_len == 0:
            l.debug("ending hook for %s at %#x with len 0", pending_info.func_name, pending_info.addr)
            chall_resp_plugin.pop_from_backup()
            return

        # result constraint
        new_var = state.se.BVS(pending_info.get_type() + "_" + str(pending_info.input_base) + "_result", 32)
        constraint = new_var == result
        chall_resp_plugin.replacement_pairs.append((new_var, state.regs.eax))
        state.regs.eax = new_var

        # finish marking the input
        input_val = state.memory.load(pending_info.input_val, real_len)
        l.debug("string len was %d, value was %d", real_len, state.se.any_int(result))
        input_bvs = state.se.BVS(pending_info.get_type() + "_" + str(pending_info.input_base) + "_input", input_val.size())
        chall_resp_plugin.str_to_int_pairs.append((input_bvs, new_var))
        if pending_info.allows_negative:
            chall_resp_plugin.allows_negative_bvs.add(input_bvs.cache_key)
        chall_resp_plugin.replacement_pairs.append((input_bvs, input_val))
    elif pending_info.get_type() == "IntToStr":
        # result constraint
        result = state.se.BVV(state.se.any_str(state.mem[pending_info.str_dst_addr].string.resolved))
        if result is None or result.size() == 0:
            l.warning("zero len string")
            chall_resp_plugin.pop_from_backup()
            return
        new_var = state.se.BVS(pending_info.get_type() + "_" + str(pending_info.input_base) + "_result",
                               result.size())
        chall_resp_plugin.replacement_pairs.append((new_var, state.mem[pending_info.str_dst_addr].string.resolved))
        state.memory.store(pending_info.str_dst_addr, new_var)
        constraint = new_var == result

        # mark the input
        input_val = pending_info.input_val
        input_bvs = state.se.BVS(pending_info.get_type() + "_" + str(pending_info.input_base) + "_input", 32)
        chall_resp_plugin.int_to_str_pairs.append((input_bvs, new_var))
        chall_resp_plugin.replacement_pairs.append((input_bvs, input_val))
        # here we need the constraint that the input was equal to the StrToInt_input
        state.add_constraints(input_bvs == input_val)

    else:
        chall_resp_plugin.backup_pending_info = []
        return

    l.debug("ending hook for %s at %#x", pending_info.func_name, pending_info.addr)
    chall_resp_plugin.vars_we_added.update(new_var.variables)
    chall_resp_plugin.vars_we_added.update(input_bvs.variables)
    # don't add constraints just add replacement
    state.se._solver.add_replacement(new_var, result, invalidate_cache=False)
    # dont add this constraint to preconstraints or we lose real constraints
    # chall_resp_plugin.tracer.preconstraints.append(constraint)
    chall_resp_plugin.tracer.variable_map[list(new_var.variables)[0]] = constraint
    chall_resp_plugin.pop_from_backup()


def exit_hook(state):
    if not state.has_plugin("chall_resp_info"):
        return

    guard = state.inspect.exit_guard

    # track the amount of stdout we had when a constraint was first added to a byte of stdin
    chall_resp_plugin = state.get_plugin("chall_resp_info")
    stdin_min_stdout_constraints = chall_resp_plugin.stdin_min_stdout_constraints
    stdout_pos = state.se.any_int(state.posix.get_file(1).pos)
    for v in guard.variables:
        if v.startswith("file_/dev/stdin"):
            byte_num = ChallRespInfo.get_byte(v)
            if byte_num not in stdin_min_stdout_constraints:
                stdin_min_stdout_constraints[byte_num] = stdout_pos

def syscall_hook(state):
    if not state.has_plugin("chall_resp_info"):
        return

    # here we detect how much stdout we have when a byte is first read in
    syscall_name = state.inspect.syscall_name
    if syscall_name == "receive":
        # track the amount of stdout we had when we first read the byte
        stdin_min_stdout_reads = state.get_plugin("chall_resp_info").stdin_min_stdout_reads
        stdout_pos = state.se.any_int(state.posix.get_file(1).pos)
        stdin_pos = state.se.any_int(state.posix.get_file(0).pos)
        for i in range(0, stdin_pos):
            if i not in stdin_min_stdout_reads:
                stdin_min_stdout_reads[i] = stdout_pos

    # here we make random preconstrained instead of concrete A's
    if syscall_name == "random":
        num_bytes = state.se.any_int(state.regs.ecx)
        buf = state.se.any_int(state.regs.ebx)
        if num_bytes != 0:
            rand_bytes = state.se.BVS("random", num_bytes*8)
            concrete_val = state.se.BVV("A"*num_bytes)
            state.se._solver.add_replacement(rand_bytes, concrete_val, invalidate_cache=False)
            state.memory.store(buf, rand_bytes)


def constraint_hook(state):
    if not state.has_plugin("chall_resp_info"):
        return

    # here we prevent adding constraints if there's a pending thing
    chall_resp_plugin = state.get_plugin("chall_resp_info")
    if chall_resp_plugin.pending_info is not None and simuvex.o.REPLACEMENT_SOLVER in state.options:
        state.inspect.added_constraints = []


class ChallRespInfo(SimStatePlugin):
    """
    This state plugin keeps track of the reads and writes to symbolic addresses
    """
    def __init__(self):
        SimStatePlugin.__init__(self)
        # for each constraint we check what the max stdin it has and how much stdout we have
        self.stdin_min_stdout_constraints = {}
        self.stdin_min_stdout_reads = {}
        self.format_infos = dict()
        self.project = None
        self.pending_info = None
        self.tracer = None
        self.str_to_int_pairs = []
        self.int_to_str_pairs = []
        self.ret_addr_to_unhook = None
        self.vars_we_added = set()
        self.replacement_pairs = []
        self.backup_pending_info = []
        self.allows_negative_bvs = set()


    def __getstate__(self):
        d = dict(self.__dict__)
        del d["project"]
        del d["tracer"]
        del d["state"]

        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self.project = None
        self.tracer = None
        self.state = None

    def copy(self):
        s = ChallRespInfo()
        s.stdin_min_stdout_constraints = dict(self.stdin_min_stdout_constraints)
        s.stdin_min_stdout_reads = dict(self.stdin_min_stdout_reads)
        s.format_infos = dict(self.format_infos)
        s.project = self.project
        s.pending_info = self.pending_info
        s.tracer = self.tracer
        s.str_to_int_pairs = list(self.str_to_int_pairs)
        s.int_to_str_pairs = list(self.int_to_str_pairs)
        s.ret_addr_to_unhook = self.ret_addr_to_unhook
        s.vars_we_added = set(self.vars_we_added)
        s.replacement_pairs = list(self.replacement_pairs)
        s.backup_pending_info = list(self.backup_pending_info)
        s.allows_negative_bvs = set(self.allows_negative_bvs)
        return s

    @staticmethod
    def get_byte(var_name):
        idx = var_name.split("_")[3]
        return int(idx, 16)

    def lookup_original(self, replacement):
        for r, o in self.replacement_pairs:
            if r is replacement:
                return o
        return None

    def pop_from_backup(self):
        # pop from pending info
        if self.backup_pending_info:
            ret_addr, pending_info = self.backup_pending_info[0]
            self.pending_info = pending_info
            self.ret_addr_to_unhook = ret_addr
            self.project.hook(ret_addr, end_info_hook, length=0)
            self.backup_pending_info = self.backup_pending_info[1:]

    def get_stdin_indices(self, variable):
        byte_indices = set()
        for str_val, int_val in self.str_to_int_pairs:
            if variable in int_val.variables:
                original_str = self.lookup_original(str_val)
                if original_str is None:
                    l.warning("original_str is None")
                    continue
                for v in original_str.variables:
                    if v.startswith("file_/dev/stdin"):
                        byte_indices.add(self.get_byte(v))
        return byte_indices

    def get_stdout_indices(self, variable):
        file_1 = self.state.posix.get_file(1)
        stdout_size = self.state.se.any_int(file_1.pos)
        stdout = file_1.content.load(0, stdout_size)
        byte_indices = set()
        for int_val, str_val in self.int_to_str_pairs:
            if variable in int_val.variables:
                num_bytes = str_val.size()/8
                if stdout.op != "Concat":
                    l.warning("stdout is not concat!")
                    continue
                stdout_pos = 0
                for arg in stdout.args:
                    if arg is str_val:
                        byte_indices.update(range(stdout_pos, stdout_pos+num_bytes))
                    stdout_pos += arg.size()/8
        return byte_indices

    def get_real_len(self, input_val, base, result_bv, allows_negative):
        # handle 0-length bv's and None's
        if input_val is None or input_val.size() == 0:
            return 0

        result = self.state.se.any_int(result_bv)
        possible_len = self.get_possible_len(input_val, base, allows_negative)
        if possible_len == 0:
            return 0
        input_s = self.state.se.any_str(input_val)
        try:
            for i in range(possible_len):
                if input_s[:i+1] == "-":
                    continue
                if int(input_s[:i+1], base) & ((1<<result_bv.size())-1) == result:
                    return i+1
        except ValueError:
            return 0
        l.warning("couldn't get real len returning 0")
        return 0

    def get_possible_len(self, input_val, base, allows_negative):
        state = self.state
        input_s = state.se.any_str(input_val)
        nums = "0123456789abcdef"
        still_whitespace=True
        for i, c in enumerate(input_s):
            if still_whitespace and c == "-" and allows_negative:
                still_whitespace = False
                continue
            if c not in string.whitespace:
                still_whitespace = False
            if still_whitespace and c in string.whitespace:
                continue
            if c.lower() not in nums[:base]:
                return i
        return len(input_s)

    def get_same_length_constraints(self):
        constraints = []
        for str_var, int_var in self.str_to_int_pairs:
            int_var_name = list(int_var.variables)[0]
            base = int(int_var_name.split("_")[1], 10)
            original_len = str_var.size()/8
            abs_max = (1 << int_var.size())-1
            if str_var.cache_key in self.allows_negative_bvs:
                abs_max = (1 << (int_var.size()-1))-1
            max_val = base**(original_len)-1
            min_val = 0
            if str_var.cache_key in self.allows_negative_bvs and original_len > 1:
                min_val = -(base**(original_len-1)-1)

            max_val = min(max_val, abs_max)
            min_val = max(min_val, -abs_max)

            constraints.append(claripy.And(int_var.SGE(min_val), int_var <= max_val))
        return constraints

    @staticmethod
    def atoi_dumps(state, require_same_length=True):
        try:
            if not state.has_plugin("chall_resp_info"):
                l.warning("no chall resp info, just using posix dumps(0)")
                return state.posix.dumps(0)

            chall_resp_plugin = state.get_plugin("chall_resp_info")

            vars_to_solve = []
            pos = state.se.any_int(state.posix.get_file(0).pos)
            stdin = state.posix.get_file(0).content.load(0, pos)
            vars_to_solve.append(stdin)

            for s_var, int_var in chall_resp_plugin.str_to_int_pairs:
                vars_to_solve.append(int_var)

            if require_same_length:
                extra_constraints = chall_resp_plugin.get_same_length_constraints()
            else:
                extra_constraints = []

            solns = state.se._solver.batch_eval(vars_to_solve, 1, extra_constraints=extra_constraints)
            if len(solns) == 0:
                if require_same_length:
                    l.warning("could not satisfy with same length, falling back to different lengths")
                    return ChallRespInfo.atoi_dumps(state, require_same_length=False)
                else:
                    return state.posix.dumps(0)
            solns = solns[0]

            # now make the real stdin
            stdin = state.se.any_str(state.se.BVV(solns[0], pos * 8))

            stdin_replacements = []
            for soln, (s_var, int_var) in zip(solns[1:], chall_resp_plugin.str_to_int_pairs):
                int_var_name = list(int_var.variables)[0]
                indices = chall_resp_plugin.get_stdin_indices(int_var_name)
                if len(indices) == 0:
                    continue
                start = min(indices)
                length = max(indices) + 1 - start
                base = int(int_var_name.split("_")[1], 10)
                str_val = int2base(soln, base)
                # pad for same length requirement
                if require_same_length and len(str_val) < length:
                    str_val = str_val.rjust(length, "0")
                    if "-" in str_val:
                        str_val = "-" + str_val.replace("-", "")
                stdin_replacements.append((start, length, str_val))

            # filter for same start with value 0
            for i in list(stdin_replacements):
                if any(ii[0] == i[0] and ii[2] != i[2] for ii in stdin_replacements):
                    if int(i[2]) == 0:
                        stdin_replacements.remove(i)

            # now do the replacing
            offset = 0
            for start, length, str_val in sorted(stdin_replacements):
                stdin = stdin[:start + offset] + str_val + stdin[start + length + offset:]
                offset = len(str_val) - length

            return stdin
        except Exception as e:
            l.error("Exception %s during atoi_dumps!!", e.message)
            return state.posix.dumps(0)

    @staticmethod
    def prep_tracer(tracer, format_infos=None):
        path = tracer.path_group.one_active
        format_infos = [] if format_infos is None else format_infos
        state = path.state
        state.inspect.b(
            'exit',
            simuvex.BP_BEFORE,
            action=exit_hook
        )
        state.inspect.b(
            'syscall',
            simuvex.BP_AFTER,
            action=syscall_hook
        )
        state.inspect.b(
            'constraints',
            simuvex.BP_BEFORE,
            action=constraint_hook
        )

        if state.has_plugin("chall_resp_info"):
            chall_resp_plugin = state.get_plugin("chall_resp_info")
        else:
            chall_resp_plugin = ChallRespInfo()
        chall_resp_plugin.project = path._project
        chall_resp_plugin.tracer = tracer
        for f in format_infos:
            chall_resp_plugin.format_infos[f.addr] = f

        state.register_plugin("chall_resp_info", chall_resp_plugin)

        for addr in chall_resp_plugin.format_infos:
            path._project.hook(addr, generic_info_hook, length=0)


# THE ZEN HOOK

def zen_hook(state, expr):
    # don't do this if inside a hooked function
    if state.has_plugin("chall_resp_info") and state.get_plugin("chall_resp_info").pending_info is not None:
        return

    if expr.op not in claripy.operations.leaf_operations and expr.op != "Concat":
        # if there is more than one symbolic argument we replace it and preconstrain it
        flag_args = ZenPlugin.get_flag_rand_args(expr)
        if len(flag_args) > 1:
            zen_plugin = state.get_plugin("zen_plugin")

            if expr.cache_key in zen_plugin.replacements:
                # we already have the replacement
                concrete_val = state.se.any_int(expr)
                replacement = zen_plugin.replacements[expr.cache_key]
                state.se._solver.add_replacement(replacement, concrete_val, invalidate_cache=False)
                zen_plugin.tracer.preconstraints.append(replacement == concrete_val)
                zen_plugin.preconstraints.append(replacement == concrete_val)
            else:
                # we need to make a new replacement
                replacement = claripy.BVS("cgc-flag-zen", expr.size())
                concrete_val = state.se.any_int(expr)
                state.se._solver.add_replacement(replacement, concrete_val, invalidate_cache=False)

                # if the depth is less than the max add the constraint and get which bytes it contains
                depth = zen_plugin.get_expr_depth(expr)
                if depth < zen_plugin.max_depth:
                    con = replacement == expr
                    state.add_constraints(con)
                    contained_bytes = zen_plugin.get_flag_bytes(expr)
                    zen_plugin.byte_dict[list(replacement.variables)[0]] = contained_bytes
                    zen_plugin.zen_constraints.append(con)
                    # saves a ton of memory to do this here rather than later
                    zen_plugin.zen_constraints.append(state.se.simplify(con))
                else:
                    # otherwise don't add the constraint, just replace
                    depth = 0
                    zen_plugin.byte_dict[list(replacement.variables)[0]] = set()

                # save and replace
                var = list(replacement.variables)[0]
                zen_plugin.depths[var] = depth
                constraint = replacement == concrete_val
                zen_plugin.tracer.preconstraints.append(constraint)
                zen_plugin.preconstraints.append(replacement == concrete_val)

                zen_plugin.replacements[expr.cache_key] = replacement

            return replacement


def zen_memory_write(state):
    mem_write_expr = state.inspect.mem_write_expr
    new_expr = zen_hook(state, mem_write_expr)
    if new_expr is not None:
        state.inspect.mem_write_expr = new_expr


def zen_register_write(state):
    reg_write_expr = state.inspect.reg_write_expr
    new_expr = zen_hook(state, reg_write_expr)
    if new_expr is not None:
        state.inspect.reg_write_expr = new_expr


class ZenPlugin(SimStatePlugin):
    def __init__(self, max_depth=13):
        SimStatePlugin.__init__(self)
        # dict from cache key to asts
        self.replacements = dict()
        # dict from zen vars to the depth
        self.depths = dict()
        # dict from zen vars to the bytes contained
        self.byte_dict = dict()
        # the tracer object (need to add replacements here)
        self.tracer = None
        # the max depth an object can have before it is replaced with a zen object with no constraint
        self.max_depth = max_depth
        # the zen replacement constraints (the ones that don't preconstrain input)
        # ie (flagA + flagB == zen1234)
        self.zen_constraints = []

        self.preconstraints = []

        self.controlled_transmits = []

    def __getstate__(self):
        d = dict(self.__dict__)
        del d["tracer"]
        del d["state"]

        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self.tracer = None
        self.state = None

    @staticmethod
    def get_flag_rand_args(expr):
        symbolic_args = tuple(a for a in expr.args if isinstance(a, claripy.ast.Base) and a.symbolic)
        flag_args = []
        for a in symbolic_args:
            if any(v.startswith("cgc-flag") or v.startswith("random") for v in a.variables):
                flag_args.append(a)
        return flag_args

    def get_expr_depth(self, expr):
        flag_args = self.get_flag_rand_args(expr)
        flag_arg_vars = set.union(*[set(v.variables) for v in flag_args])
        flag_arg_vars = set(v for v in flag_arg_vars if v.startswith("cgc-flag") or v.startswith("random"))
        if len(flag_arg_vars) == 0:
            return 0
        depth = max(self.depths.get(v, 0) for v in flag_arg_vars) + 1
        return depth

    def copy(self):
        z = ZenPlugin()
        # we explicitly don't copy the dict since it only is a mapping from var to replacement
        z.replacements = self.replacements
        # we explicitly don't copy the dict since it only is a mapping form var to depth
        z.depths = self.depths
        # explicitly don't copy
        z.byte_dict = self.byte_dict
        z.tracer = self.tracer
        z.max_depth = self.max_depth
        z.zen_constraints = self.zen_constraints
        z.preconstraints = self.preconstraints
        z.controlled_transmits = self.controlled_transmits
        return z

    def get_flag_bytes(self, ast):
        flag_args = self.get_flag_rand_args(ast)
        flag_arg_vars = set.union(*[set(v.variables) for v in flag_args])
        flag_arg_vars = set(v for v in flag_arg_vars if v.startswith("cgc-flag"))
        contained_bytes = set()
        for v in flag_arg_vars:
            if v in self.byte_dict:
                contained_bytes.update(self.byte_dict[v])
        return contained_bytes

    def filter_constraints(self, constraints):
        zen_cache_keys = set(x.cache_key for x in self.zen_constraints)
        new_cons = [ ]
        for con in constraints:
            if con.cache_key in zen_cache_keys or \
                    not all(v.startswith("cgc-flag") or
                    v.startswith("random") for v in con.variables) or \
                    len(con.variables) == 0:
                new_cons.append(con)
        return new_cons

    def analyze_transmit(self, state, buf):
        fd = state.se.any_int(state.regs.ebx)
        try:
            state.memory.permissions(state.se.any_int(buf))
        except SimMemoryError:
            l.warning("detected possible arbitary transmit to fd %d", fd)
            if fd == 0 or fd == 1:
                self.controlled_transmits.append((state.copy(), buf))

    @staticmethod
    def prep_tracer(tracer):
        state = tracer.path_group.one_active.state
        if state.has_plugin("zen_plugin"):
            zen_plugin = state.get_plugin("zen_plugin")
        else:
            zen_plugin = ZenPlugin()
        zen_plugin.tracer = tracer

        state.register_plugin("zen_plugin", zen_plugin)
        state.inspect.b(
            'reg_write',
            simuvex.BP_BEFORE,
            action=zen_register_write
        )
        state.inspect.b(
            'mem_write',
            simuvex.BP_BEFORE,
            action=zen_memory_write
        )

        # setup the byte dict
        byte_dict = zen_plugin.byte_dict
        for i, b in enumerate(tracer.cgc_flag_bytes):
            var = list(b.variables)[0]
            byte_dict[var] = {i}

        tracer.preconstraints.extend(zen_plugin.preconstraints)
