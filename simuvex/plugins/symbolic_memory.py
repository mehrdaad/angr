#!/usr/bin/env python

import logging
import itertools
import cooldict

l = logging.getLogger("simuvex.plugins.symbolic_memory")

import claripy
from ..storage.memory import SimMemory
from ..storage.paged_memory import SimPagedMemory
from ..storage.memory_object import SimMemoryObject

class SimSymbolicMemory(SimMemory): #pylint:disable=abstract-method
    def __init__(self, backer=None, mem=None, memory_id="mem", repeat_min=None, repeat_constraints=None, repeat_expr=None):
        SimMemory.__init__(self)
        if backer is not None and not isinstance(backer, cooldict.FinalizableDict):
            backer = cooldict.FinalizableDict(storage=backer)
            backer.finalize()
        self.mem = SimPagedMemory(backer=backer) if mem is None else mem
        self.id = memory_id

        # for the norepeat stuff
        self._repeat_constraints = [ ] if repeat_constraints is None else repeat_constraints
        self._repeat_expr = repeat_expr
        self._repeat_granularity = 0x10000
        self._repeat_min = 0x13370000 if repeat_min is None else repeat_min

        # default strategies
        self._default_read_strategy = ['symbolic', 'any']
        self._default_write_strategy = [ 'norepeats',  'any' ]
        self._default_symbolic_write_strategy = [ 'symbolic_nonzero', 'any' ]
        self._write_address_range = 1

        #
        # These are some preformance-critical thresholds
        #

        # The maximum range of a symbolic write address. If an address range is greater than this number,
        # SimMemory will simply concretize it.
        self._symbolic_write_address_range = 17

        # The maximum range of a symbolic read address. If an address range is greater than this number,
        # SimMemory will simply concretize it.
        self._read_address_range = 1024

        # The maximum size of a symbolic-sized operation. If a size maximum is greater than this number,
        # SimMemory will constrain it to this number. If the size minimum is greater than this
        # number, a SimMemoryLimitError is thrown.
        self._maximum_symbolic_size = 8 * 1024

    def set_state(self, s):
        SimMemory.set_state(self, s)
        self.mem.state = s

    def _ana_getstate(self):
        d = self.__dict__.copy()
        d['concrete'] = {}
        for addr in self.mem:
            b = self.mem[addr]
            if isinstance(b, str):
                d['concrete'][addr] = ord(b)
            elif isinstance(b, SimMemoryObject):
                b = b.bytes_at(addr, 1)
                d['concrete'][addr] = self.state.se.any_int(b)
        return d

    #
    # Symbolicizing!
    #

    def make_symbolic(self, name, addr, length=None):
        '''
        Replaces length bytes, starting at addr, with a symbolic variable named
        name. Adds a constraint equaling that symbolic variable to the value
        previously at addr, and returns the variable.
        '''
        l.debug("making %s bytes symbolic", length)

        if isinstance(addr, str):
            addr, length = self.state.arch.registers[addr]
        else:
            if length is None:
                raise Exception("Unspecified length!")

        r, read_constraints = self.load(addr, length)
        l.debug("... read constraints: %s", read_constraints)
        self.state.add_constraints(*read_constraints)

        v = self.state.se.Unconstrained(name, r.size())
        write_constraints = self.store(addr, v)
        self.state.add_constraints(*write_constraints)
        l.debug("... write constraints: %s", write_constraints)
        self.state.add_constraints(r == v)
        l.debug("... eq constraints: %s", r == v)
        return v

    #
    # Address concretization
    #

    def _symbolic_size_range(self, size):
        max_size = self.state.se.max_int(size)
        min_size = self.state.se.min_int(size)

        if min_size > self._maximum_symbolic_size:
            self.state.log.add_event('memory_limit', message="Symbolic size outside of allowable limits", size=size)
            if options.BEST_EFFORT_MEMORY_STORING not in self.state.options:
                raise SimMemoryLimitError("Symbolic size outside of allowable limits")
            else:
                min_size = self._maximum_symbolic_size

        return min_size, min(max_size, self._maximum_symbolic_size)

    def _concretize_strategy(self, v, s, limit, cache):
        r = None
        #if s == "norepeats_simple":
        #    if self.state.se.solution(v, self._repeat_min):
        #        l.debug("... trying super simple method.")
        #        r = [ self._repeat_min ]
        #        self._repeat_min += self._repeat_granularity
        #elif s == "norepeats_range":
        #    l.debug("... trying ranged simple method.")
        #    r = [ self.state.se.any_int(v, extra_constraints = [ v > self._repeat_min, v < self._repeat_min + self._repeat_granularity ]) ]
        #    self._repeat_min += self._repeat_granularity
        #elif s == "norepeats_min":
        #    l.debug("... just getting any value.")
        #    r = [ self.state.se.any_int(v, extra_constraints = [ v > self._repeat_min ]) ]
        #    self._repeat_min = r[0] + self._repeat_granularity
        if s == "norepeats":
            if self._repeat_expr is None:
                self._repeat_expr = self.state.se.Unconstrained("%s_repeat" % self.id, self.state.arch.bits)

            c = self.state.se.any_int(v, extra_constraints=self._repeat_constraints + [ v == self._repeat_expr ])
            self._repeat_constraints.append(self._repeat_expr != c)
            r = [ c ]
        elif s == "symbolic":
            # if the address concretizes to less than the threshold of values, try to keep it symbolic
            mx = self.state.se.max_int(v)
            mn = self.state.se.min_int(v)

            cache['max'] = mx
            cache['min'] = mn
            cache['solutions'].add(mx)
            cache['solutions'].add(mn)

            l.debug("... range is (%d, %d)", mn, mx)
            if mx - mn < limit:
                l.debug("... generating %d addresses", limit)
                r = self.state.se.any_n_int(v, limit)
                l.debug("... done")
        elif s == "symbolic_nonzero":
            # if the address concretizes to less than the threshold of values, try to keep it symbolic
            mx = self.state.se.max_int(v, extra_constraints=[v != 0])
            mn = self.state.se.min_int(v, extra_constraints=[v != 0])

            cache['max'] = mx
            cache['solutions'].add(mx)
            cache['solutions'].add(mn)

            l.debug("... range is (%d, %d)", mn, mx)
            if mx - mn < limit:
                l.debug("... generating %d addresses", limit)
                r = self.state.se.any_n_int(v, limit)
                l.debug("... done")
        elif s == "any":
            r = [ cache['solutions'].__iter__().next() ]

        return r, cache

    def _concretize_addr(self, v, strategy, limit):
        # if there's only one option, let's do it
        if not self.state.se.symbolic(v):
            l.debug("... concrete value")
            return [ self.state.se.any_int(v) ]

        if not self.state.satisfiable():
            raise SimMemoryError("Trying to concretize with unsat constraints.")

        l.debug("... concretizing address with limit %d", limit)

        cache = { }
        cache['solutions'] = { self.state.se.any_int(v) }

        for s in strategy:
            l.debug("... trying strategy %s", s)
            try:
                result, cache = self._concretize_strategy(v, s, limit, cache)
                if result is not None:
                    return result
                else:
                    l.debug("... failed (with None)")
            except SimUnsatError:
                l.debug("... failed (with exception)")
                continue

        raise SimMemoryError("Unable to concretize address with the provided strategy.")

    def concretize_write_addr(self, addr, strategy=None, limit=None):
        if isinstance(addr, (int, long)):
            return [addr]

        #l.debug("concretizing addr: %s with variables", addr.variables)
        if strategy is None:
            if any([ "multiwrite" in c for c in self.state.se.variables(addr) ]):
                l.debug("... defaulting to symbolic write!")
                strategy = self._default_symbolic_write_strategy
                limit = self._symbolic_write_address_range if limit is None else limit
            else:
                l.debug("... defaulting to concrete write!")
                strategy = self._default_write_strategy
                limit = self._write_address_range if limit is None else limit
        limit = self._write_address_range if limit is None else limit

        return self._concretize_addr(addr, strategy=strategy, limit=limit)

    def concretize_read_addr(self, addr, strategy=None, limit=None):
        '''
        Concretizes an address meant for reading.

            @param addr: an expression for the address
            @param strategy: the strategy to use for concretization
            @param limit: how many concrete values to limit the concretization to

            @returns a list of concrete addresses
        '''
        if isinstance(addr, (int, long)):
            return [addr]
        strategy = self._default_read_strategy if strategy is None else strategy
        limit = self._read_address_range if limit is None else limit

        return self._concretize_addr(addr, strategy=strategy, limit=limit)

    def normalize_address(self, addr):
        return self.concretize_read_addr(addr)

    #
    # Memory reading
    #

    def _read_from(self, addr, num_bytes):
        missing = [ ]
        the_bytes = { }
        if num_bytes <= 0:
            raise SimMemoryError('Trying to load %x bytes from symbolic memory %s' % (num_bytes, self.id))

        l.debug("Reading from memory at %d", addr)
        for i in range(0, num_bytes):
            try:
                b = self.mem[addr+i]
                if isinstance(b, (int, long, str)):
                    b = self.state.BVV(b, 8)
                the_bytes[i] = b
            except KeyError:
                missing.append(i)

        l.debug("... %d found, %d missing", len(the_bytes), len(missing))

        if len(missing) > 0:
            name = "%s_%x" % (self.id, addr)
            b = self.state.se.Unconstrained(name, num_bytes*8)
            if self.id == 'reg' and self.state.arch.register_endness == 'Iend_LE':
                b = b.reversed
            if self.id == 'mem' and self.state.arch.memory_endness == 'Iend_LE':
                b = b.reversed

            self.state.log.add_event('uninitialized', memory_id=self.id, addr=addr, size=num_bytes)
            default_mo = SimMemoryObject(b, addr)
            for m in missing:
                the_bytes[m] = default_mo
                self.mem[addr+m] = default_mo

        buf = [ ]
        buf_size = 0
        last_expr = None
        for i,e in itertools.chain(sorted(list(the_bytes.iteritems()), key=lambda x: x[0]), [(num_bytes, None)]):
            if not isinstance(e, SimMemoryObject) or e is not last_expr:
                if isinstance(last_expr, claripy.A):
                    buf.append(last_expr)
                    buf_size += 1
                elif isinstance(last_expr, SimMemoryObject):
                    buf.append(last_expr.bytes_at(addr+buf_size, i-buf_size))
                    buf_size = i
            last_expr = e

        if len(buf) > 1:
            r = self.state.se.Concat(*buf)
        else:
            r = buf[0]
        return r

    def _load(self, dst, size, condition=None, fallback=None):
        if isinstance(size, (int, long)):
            size = self.state.BVV(size, self.state.arch.bits)

        if self.state.se.symbolic(size):
            l.warning("Concretizing symbolic length. Much sad; think about implementing.")

        # for now, we always load the maximum size
        _,max_size = self._symbolic_size_range(size)
        if options.ABSTRACT_MEMORY not in self.state.options:
            self.state.add_constraints(size == max_size)
        size = self.state.se.BVV(max_size, self.state.arch.bits)

        if max_size == 0:
            self.state.log.add_event('memory_limit', message="0-length read")
            raise SimMemoryLimitError("0-length read")

        size = self.state.se.any_int(size)
        if self.state.se.symbolic(dst) and options.AVOID_MULTIVALUED_READS in self.state.options:
            return self.state.se.Unconstrained("symbolic_read", size*8), [ ]

        # get a concrete set of read addresses
        addrs = self.concretize_read_addr(dst)

        read_value = self._read_from(addrs[0], size)
        constraint_options = [ dst == addrs[0] ]

        for a in addrs[1:]:
            read_value = self.state.se.If(dst == a, self._read_from(a, size), read_value)
            constraint_options.append(dst == a)

        if len(constraint_options) > 1:
            load_constraint = self.state.se.Or(*constraint_options)
        else:
            load_constraint = constraint_options[0]

        if condition is not None:
            read_value = self.state.se.If(condition, read_value, fallback)
            load_constraint = self.state.se.Or(self.state.se.And(condition, load_constraint), self.state.se.Not(condition))

        return read_value, [ load_constraint ]

    def _find(self, start, what, max_search=None, max_symbolic_bytes=None, default=None):
        if isinstance(start, (int, long)):
            start = self.state.BVV(start, self.state.arch.bits)

        constraints = [ ]
        remaining_symbolic = max_symbolic_bytes
        seek_size = len(what)/8
        symbolic_what = what.symbolic
        l.debug("Search for %d bytes in a max of %d...", seek_size, max_search)

        preload = True
        all_memory = self.state.mem_expr(start, max_search, endness="Iend_BE")
        if all_memory.symbolic:
            preload = False

        cases = [ ]
        match_indices = [ ]
        for i in itertools.count():
            l.debug("... checking offset %d", i)
            if i > max_search - seek_size:
                l.debug("... hit max size")
                break
            if remaining_symbolic is not None and remaining_symbolic == 0:
                l.debug("... hit max symbolic")
                break

            if preload:
                b = all_memory[max_search*8 - i*8 - 1 : max_search*8 - i*8 - seek_size*8]
            else:
                b = self.state.mem_expr(start + i, seek_size, endness="Iend_BE")
            cases.append([ b == what, start + i ])
            match_indices.append(i)

            if not b.symbolic and not symbolic_what:
                #print "... checking", b, 'against', what
                if self.state.se.any_int(b) == self.state.se.any_int(what):
                    l.debug("... found concrete")
                    break
            else:
                if remaining_symbolic is not None:
                    remaining_symbolic -= 1

        if default is None:
            l.debug("... no default specified")
            default = 0
            constraints += [ self.state.se.Or(*[ c for c,_ in cases]) ]

        #l.debug("running ite_cases %s, %s", cases, default)
        r = self.state.se.ite_cases(cases, default)
        return r, constraints, match_indices

    def __contains__(self, dst):
        if isinstance(dst, (int, long)):
            addr = dst
        elif self.state.se.symbolic(dst):
            try:
                addr = self._concretize_addr(dst, strategy=['allocated'], limit=1)[0]
            except SimMemoryError:
                return False
        else:
            addr = self.state.se.any_int(dst)
        return addr in self.mem

    #
    # Writes
    #

    def _write_to(self, addr, cnt, size=None, condition=None, fallback=None):
        size_bits = len(cnt)
        size_bytes = size_bits/8
        constraints = [ ]

        # here, we ensure the uuids are generated for every expression written to memory
        cnt.make_uuid()

        # handle conditional writes
        if condition is not None:
            fallback_cnt = self._read_from(addr, size_bytes) if fallback is None else fallback
            conditioned_cnt = self.state.se.If(condition, cnt, fallback_cnt)
        else:
            conditioned_cnt = cnt

        # handle symbolically-sized writes
        if size is not None:
            befores = self._read_from(addr, size_bytes).chop(bits=8)
            afters = conditioned_cnt.chop(bits=8)
            if size_bytes == 1:
                sized_cnt = self.state.se.If(self.state.se.UGT(size, 0), afters[0], befores[0])
            else:
                sized_cnt = self.state.se.Concat(*[self.state.se.If(self.state.se.UGT(size, i), a, b) for i,(a,b) in enumerate(zip(afters,befores))])

            constraints += [ self.state.se.ULE(size, size_bytes) ]
        else:
            sized_cnt = conditioned_cnt

        mo = SimMemoryObject(sized_cnt, addr, length=size_bytes)
        for actual_addr in range(addr, addr + mo.length):
            l.debug("... updating mappings")
            l.debug("... writing 0x%x", actual_addr)
            self.mem[actual_addr] = mo

        return constraints

    def _store(self, dst, cnt, size=None, condition=None, fallback=None):
        l.debug("Doing a store...")

        if size is not None and self.state.se.symbolic(size) and options.AVOID_MULTIVALUED_WRITES in self.state.options:
            return [ ]

        if self.state.se.symbolic(dst) and options.AVOID_MULTIVALUED_WRITES in self.state.options:
            return [ ]

        addrs = self.concretize_write_addr(dst)
        if len(addrs) == 1:
            l.debug("... concretized to 0x%x", addrs[0])
            constraint = [ dst == addrs[0] ]
        else:
            l.debug("... concretized to %d values", len(addrs))
            constraint = [ self.state.se.Or(*[ dst == a for a in addrs ])  ]

        if isinstance(size, (int, long)):
            size = self.state.se.BVV(size, self.state.arch.bits)

        if len(addrs) == 1:
            c = self._write_to(addrs[0], cnt, size=size, condition=condition, fallback=fallback)
            constraint += c
        else:
            l.debug("... many writes")
            if size is None:
                length_expr = len(cnt)/8 # pylint:disable=maybe-no-member
            else:
                length_expr = size

            for a in addrs:
                ite_length = self.state.se.If(dst == a, length_expr, self.state.BVV(0))
                c = self._write_to(a, cnt, size=ite_length, condition=condition, fallback=fallback)
                constraint += c

        l.debug("... done")
        return constraint

    def store_with_merge(self, dst, cnt, size=None, condition=None, fallback=None): #pylint:disable=unused-argument
        if options.ABSTRACT_MEMORY not in self.state.options:
            raise SimMemoryError('store_with_merge is not supported without abstract memory.')

        l.debug("Doing a store with merging...")

        addrs = self.concretize_write_addr(dst)

        if len(addrs) == 1:
            l.debug("... concretized to 0x%x", addrs[0])
        else:
            l.debug("... concretized to %d values", len(addrs))

        if size is None:
            # Full length
            length = len(cnt)
        else:
            raise NotImplementedError()

        for addr in addrs:
            # First we load old values
            old_val = self._read_from(addr, length / 8)
            assert isinstance(old_val, claripy.A)

            # FIXME: This is a big hack
            def is_reversed(o):
                if isinstance(o, claripy.A) and o.op == 'Reverse':
                    return True
                return False

            def can_be_reversed(o):
                if isinstance(o, claripy.A) and (isinstance(o.model, claripy.BVV) or \
                                     (isinstance(o.model, claripy.StridedInterval) and o.model.is_integer())):
                    return True
                return False

            reverse_it = False
            if is_reversed(cnt):
                if is_reversed(old_val):
                    cnt = cnt.args[0]
                    old_val = old_val.args[0]
                    reverse_it = True
                elif can_be_reversed(old_val):
                    cnt = cnt.args[0]
                    reverse_it = True
            if isinstance(old_val, (int, long, claripy.BVV)):
                merged_val = self.state.StridedInterval(bits=len(old_val), to_conv=old_val)
            else:
                merged_val = old_val
            merged_val = merged_val.union(cnt)
            if reverse_it:
                merged_val = merged_val.reversed

            # Write the new value
            self.store(addr, merged_val, size=size)

        return []

    # Return a copy of the SimMemory
    def copy(self):
        #l.debug("Copying %d bytes of memory with id %s." % (len(self.mem), self.id))
        c = SimSymbolicMemory(mem=self.mem.branch(),
                              memory_id=self.id,
                              repeat_min=self._repeat_min,
                              repeat_constraints=self._repeat_constraints,
                              repeat_expr=self._repeat_expr)
        return c

    # Unconstrain a byte
    def unconstrain_byte(self, addr):
        unconstrained_byte = self.state.se.Unconstrained("%s_unconstrain_0x%x" % (self.id, addr), 8)
        self.store(addr, unconstrained_byte)

    # Replaces the differences between self and other with unconstrained bytes.
    def unconstrain_differences(self, other):
        changed_bytes = self.changed_bytes(other)
        l.debug("Will unconstrain %d %s bytes", len(changed_bytes), self.id)
        for b in changed_bytes:
            self.unconstrain_byte(b)

    # Merge this SimMemory with the other SimMemory
    def merge(self, others, flag, flag_values):
        changed_bytes = set()

        for o in others:  # pylint:disable=redefined-outer-name
            self._repeat_constraints += o._repeat_constraints
            changed_bytes |= self.changed_bytes(o)

        if options.FRESHNESS_ANALYSIS in self.state.options:
            ignored_var_changed_bytes = set()

            if self.id == 'reg':
                fresh_vars = self.state.scratch.ignored_variables.register_variables

                for v in fresh_vars:
                    offset, size = v.reg, v.size
                    ignored_var_changed_bytes |= set(xrange(offset, offset + size))

            else:
                fresh_vars = self.state.scratch.ignored_variables.memory_variables

                for v in fresh_vars:
                    region_id, offset, _, _ = v.addr
                    size = v.size

                    if region_id == self.id:
                        ignored_var_changed_bytes |= set(range(offset, offset + size))

            changed_bytes = changed_bytes - ignored_var_changed_bytes

        l.info("Merging %d bytes", len(changed_bytes))
        l.info("... %s has changed bytes %s", self.id, changed_bytes)

        merging_occurred = len(changed_bytes) > 0
        self._repeat_min = max(other._repeat_min for other in others)

        self._merge(others, changed_bytes, flag, flag_values)

        # Generate constraints
        if options.ABSTRACT_MEMORY in self.state.options:
            constraints = []
        else:
            constraints = [self.state.se.Or(*[flag == fv for fv in flag_values])]

        return merging_occurred, constraints

    def widen(self, others, merge_flag, flag_values):

        widening_occurred = False
        changed_bytes = set()

        for o in others:  # pylint:disable=redefined-outer-name
            self._repeat_constraints += o._repeat_constraints
            changed_bytes |= self.changed_bytes(o)

        if options.FRESHNESS_ANALYSIS in self.state.options:
            ignored_var_changed_bytes = set()

            if self.id == 'reg':
                fresh_vars = self.state.scratch.ignored_variables.register_variables

                for v in fresh_vars:
                    offset, size = v.reg, v.size
                    ignored_var_changed_bytes |= set(xrange(offset, offset + size))

            else:
                fresh_vars = self.state.scratch.ignored_variables.memory_variables

                for v in fresh_vars:
                    region_id, offset, _, _ = v.addr
                    size = v.size

                    if region_id == self.id:
                        ignored_var_changed_bytes |= set(range(offset, offset + size))

            changed_bytes = changed_bytes - ignored_var_changed_bytes

        widening_occurred = (len(changed_bytes) > 0)

        l.info("Memory %s widening bytes %s", self.id, changed_bytes)

        # TODO: How to properly set the flag and flag_values?
        self._merge(others, changed_bytes, merge_flag, flag_values, is_widening=True)

        return widening_occurred

    def _merge(self, others, changed_bytes, flag, flag_values, is_widening=False):

        all_memories = [self] + others

        merged_to = None
        merged_objects = set()
        for b in sorted(changed_bytes):
            if merged_to is not None and not b >= merged_to:
                l.info("merged_to = %d ... already merged byte 0x%x", merged_to, b)
                continue
            l.debug("... on byte 0x%x", b)

            memory_objects = []
            unconstrained_in = []

            # first get a list of all memory objects at that location, and
            # all memories that don't have those bytes
            for sm, fv in zip(all_memories, flag_values):
                if b in sm.mem:
                    l.info("... present in %s", fv)
                    memory_objects.append((sm.mem[b], fv))
                else:
                    l.info("... not present in %s", fv)
                    unconstrained_in.append((sm, fv))

            mo_bases = set(mo.base for mo, _ in memory_objects)
            mo_lengths = set(mo.length for mo, _ in memory_objects)

            if len(unconstrained_in) == 0 and len(set(memory_objects) - merged_objects) == 0:
                continue

            # first, optimize the case where we are dealing with the same-sized memory objects
            if len(mo_bases) == 1 and len(mo_lengths) == 1 and len(unconstrained_in) == 0:
                our_mo = self.mem[b]
                to_merge = [(mo.object, fv) for mo, fv in memory_objects]
                merged_val = self._merge_values(to_merge, memory_objects[0][0].length, flag, is_widening=is_widening)

                # do the replacement
                self.mem.replace_memory_object(our_mo, merged_val)
                merged_objects.update(memory_objects)
            else:
                # get the size that we can merge easily. This is the minimum of
                # the size of all memory objects and unallocated spaces.
                min_size = min([mo.length - (b - mo.base) for mo, _ in memory_objects])
                for um, _ in unconstrained_in:
                    for i in range(0, min_size):
                        if b + i in um:
                            min_size = i
                            break
                merged_to = b + min_size
                l.info("... determined minimum size of %d", min_size)

                # Now, we have the minimum size. We'll extract/create expressions of that
                # size and merge them
                extracted = [(mo.bytes_at(b, min_size), fv) for mo, fv in memory_objects] if min_size != 0 else []
                created = [(self.state.se.Unconstrained("merge_uc_%s_%x" % (uc.id, b), min_size * 8), fv) for uc, fv in
                           unconstrained_in]
                to_merge = extracted + created

                merged_val = self._merge_values(to_merge, min_size, flag, is_widening=is_widening)
                self.store(b, merged_val)

    @staticmethod
    def _is_uninitialized(a):
        if isinstance(a, claripy.A) and isinstance(a.model, claripy.StridedInterval):
            return a.model.uninitialized
        return False

    def _merge_values(self, to_merge, merged_size, merge_flag, is_widening=False):
            if options.ABSTRACT_MEMORY in self.state.options:
                if self.id == 'reg' and self.state.arch.register_endness == 'Iend_LE':
                    should_reverse = True
                else:
                    should_reverse = False

                merged_val = to_merge[0][0]

                if should_reverse: merged_val = merged_val.reversed

                for tm,_ in to_merge[1:]:
                    if should_reverse: tm = tm.reversed

                    if self._is_uninitialized(tm):
                        continue
                    if is_widening:
                        l.info("Widening %s %s...", merged_val.model, tm.model)
                        merged_val = merged_val.widen(tm)
                        l.info('... Widened to %s', merged_val.model)
                    else:
                        l.info("Merging %s %s...", merged_val.model, tm.model)
                        merged_val = merged_val.union(tm)
                        l.info("... Merged to %s", merged_val.model)

                if should_reverse: merged_val = merged_val.reversed
            else:
                merged_val = self.state.BVV(0, merged_size*8)
                for tm,fv in to_merge:
                    l.debug("In merge: %s if flag is %s", tm, fv)
                    merged_val = self.state.se.If(merge_flag == fv, tm, merged_val)

            return merged_val

    def concrete_parts(self):
        '''
        Return a dict containing the concrete values in memory.
        '''
        d = { }
        for k,v in self.mem.iteritems():
            if not self.state.se.symbolic(v):
                d[k] = self.state.se.any_expr(v)

        return d

    def dbg_print(self, indent=0):
        '''
        Print out debugging information.
        '''
        lst = []
        more_data = False
        for i, addr in enumerate(self.mem.iterkeys()):
            lst.append(addr)
            if i >= 20:
                more_data = True
                break

        for addr in sorted(lst):
            data = self.mem[addr]
            if isinstance(data, SimMemoryObject):
                memobj = data
                print "%s%xh: (%s)[%d]" % (" " * indent, addr, memobj, addr - memobj.base)
            else:
                print "%s%xh: <default data>" % (" " * indent, addr)
        if more_data:
            print "%s..." % (" " * indent)

    def _copy_contents(self, dst, src, size, condition=None, src_memory=None):
        src_memory = self if src_memory is None else src_memory

        _,max_size = self._symbolic_size_range(size)
        if max_size == 0:
            return None, [ ]

        data, read_constraints = src_memory.load(src, size)
        write_constraints = self.store(dst, data, size=size, condition=condition)
        return data, read_constraints + write_constraints

    #
    # Things that are actually handled by SimPagedMemory
    #

    def changed_bytes(self, other):
        '''
        Gets the set of changed bytes between self and other.

        @param other: the other SimSymbolicMemory
        @returns a set of differing bytes
        '''
        return self.mem.changed_bytes(other.mem)

    def replace_all(self, old, new):
        '''
        Replaces all instances of expression old with expression new.

            @param old: a claripy expression. Must contain at least one named variable (to make
                        to make it possible to use the name index for speedup)
            @param new: the new variable to replace it with
        '''

        return self.mem.replace_all(old, new)

    def addrs_for_name(self, n):
        '''
        Returns addresses that contain expressions that contain a variable
        named n.
        '''
        return self.mem.addrs_for_name(n)

    def addrs_for_hash(self, h):
        '''
        Returns addresses that contain expressions that contain a variable
        with the hash of h.
        '''
        return self.mem.addrs_for_hash(h)

    def replace_memory_object(self, old, new_content):
        '''
        Replaces the memory object 'old' with a new memory object containing
        'new_content'.

            @param old: a SimMemoryObject (i.e., one from memory_objects_for_hash() or
                        memory_objects_for_name())
            @param new_content: the content (claripy expression) for the new memory object
        '''
        return self.mem.replace_memory_object(old, new_content)

    def memory_objects_for_name(self, n):
        '''
        Returns a set of SimMemoryObjects that contain expressions that contain a variable
        with the name of n. This is useful for replacing those values, in one fell swoop,
        with replace_memory_object(), even if they've been partially overwritten.
        '''
        return self.mem.memory_objects_for_name(n)

    def memory_objects_for_hash(self, n):
        '''
        Returns a set of SimMemoryObjects that contain expressions that contain a variable
        with the hash of h. This is useful for replacing those values, in one fell swoop,
        with replace_memory_object(), even if they've been partially overwritten.
        '''
        return self.mem.memory_objects_for_hash(n)

SimSymbolicMemory.register_default('memory', SimSymbolicMemory)
SimSymbolicMemory.register_default('registers', SimSymbolicMemory)
from ..s_errors import SimUnsatError, SimMemoryError, SimMemoryLimitError
from .. import s_options as options
