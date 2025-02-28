#!/usr/bin/env python3
#
# Copyright (c) 2017 Intel Corporation
# Copyright (c) 2018 Foundries.io
#
# SPDX-License-Identifier: Apache-2.0
#

import argparse
import struct
import sys
import os
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

ISR_FLAG_DIRECT = 1 << 0

# The below few hardware independent magic numbers represent various
# levels of interrupts in a multi-level interrupt system.
# 0x000000FF - represents the 1st level (i.e. the interrupts
#              that directly go to the processor).
# 0x0000FF00 - represents the 2nd level (i.e. the interrupts funnel
#              into 1 line which then goes into the 1st level)
# 0x00FF0000 - represents the 3rd level (i.e. the interrupts funnel
#              into 1 line which then goes into the 2nd level)
INTERRUPT_LVL_BITMASK = [0x000000FF, 0x0000FF00, 0x00FF0000]

INTERRUPT_BITS = [8, 8, 8]

def debug(text):
    if args.debug:
        sys.stdout.write(os.path.basename(sys.argv[0]) + ": " + text + "\n")

def error(text):
    sys.exit(os.path.basename(sys.argv[0]) + ": error: " + text + "\n")

def endian_prefix():
    if args.big_endian:
        return ">"
    else:
        return "<"

def read_intlist(elfobj, syms, snames):
    """read a binary file containing the contents of the kernel's .intList
    section. This is an instance of a header created by
    include/zephyr/linker/intlist.ld:

     struct {
       uint32_t num_vectors;       <- typically CONFIG_NUM_IRQS
       struct _isr_list isrs[]; <- Usually of smaller size than num_vectors
    }

    Followed by instances of struct _isr_list created by IRQ_CONNECT()
    calls:

    struct _isr_list {
        /** IRQ line number */
        int32_t irq;
        /** Flags for this IRQ, see ISR_FLAG_* definitions */
        int32_t flags;
        /** ISR to call */
        void *func;
        /** Parameter for non-direct IRQs */
        const void *param;
    };
    """
    intList_sect = None
    intlist = {}

    prefix = endian_prefix()

    intlist_header_fmt = prefix + "II"
    if "CONFIG_64BIT" in syms:
        intlist_entry_fmt = prefix + "iiQQ"
    else:
        intlist_entry_fmt = prefix + "iiII"

    for sname in snames:
        intList_sect = elfobj.get_section_by_name(sname)
        if intList_sect is not None:
            debug("Found intlist section: \"{}\"".format(sname))
            break

    if intList_sect is None:
        error("Cannot find the intlist section!")

    intdata = intList_sect.data()

    header_sz = struct.calcsize(intlist_header_fmt)
    header = struct.unpack_from(intlist_header_fmt, intdata, 0)
    intdata = intdata[header_sz:]

    debug(str(header))

    intlist["num_vectors"]    = header[0]
    intlist["offset"]         = header[1]

    intlist["interrupts"] = [i for i in
            struct.iter_unpack(intlist_entry_fmt, intdata)]

    debug("Configured interrupt routing")
    debug("handler    irq flags param")
    debug("--------------------------")

    for irq in intlist["interrupts"]:
        debug("{0:<10} {1:<3} {2:<3}   {3}".format(
            hex(irq[2]), irq[0], irq[1], hex(irq[3])))

    return intlist


def parse_args():
    global args

    parser = argparse.ArgumentParser(description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)

    parser.add_argument("-e", "--big-endian", action="store_true",
            help="Target encodes data in big-endian format (little endian is "
                 "the default)")
    parser.add_argument("-d", "--debug", action="store_true",
            help="Print additional debugging information")
    parser.add_argument("-o", "--output-source", required=True,
            help="Output source file")
    parser.add_argument("-k", "--kernel", required=True,
            help="Zephyr kernel image")
    parser.add_argument("-s", "--sw-isr-table", action="store_true",
            help="Generate SW ISR table")
    parser.add_argument("-V", "--vector-table", action="store_true",
            help="Generate vector table")
    parser.add_argument("-i", "--intlist-section", action="append", required=True,
            help="The name of the section to search for the interrupt data. "
                 "This is accumulative argument. The first section found would be used.")

    args = parser.parse_args()

source_assembly_header = """
#ifndef ARCH_IRQ_VECTOR_JUMP_CODE
#error "ARCH_IRQ_VECTOR_JUMP_CODE not defined"
#endif
"""

def get_symbol_from_addr(syms, addr):
    for key, value in syms.items():
        if addr == value:
            return key
    return None

def write_code_irq_vector_table(fp, vt, nv, syms):
    fp.write(source_assembly_header)

    fp.write("void __irq_vector_table __attribute__((naked)) _irq_vector_table(void) {\n")
    for i in range(nv):
        func = vt[i]

        if isinstance(func, int):
            func_as_string = get_symbol_from_addr(syms, func)
        else:
            func_as_string = func

        fp.write("\t__asm(ARCH_IRQ_VECTOR_JUMP_CODE({}));\n".format(func_as_string))
    fp.write("}\n")

def write_address_irq_vector_table(fp, vt, nv):
    fp.write("uintptr_t __irq_vector_table _irq_vector_table[%d] = {\n" % nv)
    for i in range(nv):
        func = vt[i]

        if isinstance(func, int):
            fp.write("\t{},\n".format(vt[i]))
        else:
            fp.write("\t((uintptr_t)&{}),\n".format(vt[i]))

    fp.write("};\n")

source_header = """
/* AUTO-GENERATED by gen_isr_tables.py, do not edit! */

#include <zephyr/toolchain.h>
#include <zephyr/linker/sections.h>
#include <zephyr/sw_isr_table.h>
#include <zephyr/arch/cpu.h>

typedef void (* ISR)(const void *);
"""

def write_shared_table(fp, shared, nv):
    fp.write("struct z_shared_isr_table_entry __shared_sw_isr_table"
            " z_shared_sw_isr_table[%d] = {\n" % nv)

    for i in range(nv):
        client_num = shared[i][1]
        client_list = shared[i][0]

        if not client_num:
            fp.write("\t{ },\n")
        else:
            fp.write(f"\t{{ .client_num = {client_num}, .clients = {{ ")
            for j in range(0, client_num):
                routine = client_list[j][1]
                arg = client_list[j][0]

                fp.write(f"{{ .isr = (ISR){ hex(routine) if isinstance(routine, int) else routine }, "
                        f".arg = (const void *){hex(arg)} }},")

            fp.write(" },\n},\n")

    fp.write("};\n")

def write_source_file(fp, vt, swt, intlist, syms, shared):
    fp.write(source_header)

    nv = intlist["num_vectors"]

    if "CONFIG_SHARED_INTERRUPTS" in syms:
        write_shared_table(fp, shared, nv)

    if vt:
        if "CONFIG_IRQ_VECTOR_TABLE_JUMP_BY_ADDRESS" in syms:
            write_address_irq_vector_table(fp, vt, nv)
        elif "CONFIG_IRQ_VECTOR_TABLE_JUMP_BY_CODE" in syms:
            write_code_irq_vector_table(fp, vt, nv, syms)
        else:
            error("CONFIG_IRQ_VECTOR_TABLE_JUMP_BY_{ADDRESS,CODE} not set")

    if not swt:
        return

    fp.write("struct _isr_table_entry __sw_isr_table _sw_isr_table[%d] = {\n"
            % nv)

    level2_offset = syms.get("CONFIG_2ND_LVL_ISR_TBL_OFFSET")
    level3_offset = syms.get("CONFIG_3RD_LVL_ISR_TBL_OFFSET")

    for i in range(nv):
        param = "{0:#x}".format(swt[i][0])
        func = swt[i][1]

        if isinstance (func, str) and "z_shared_isr" in func:
            param = "&z_shared_sw_isr_table[{0}]".format(i)
        if isinstance(func, int):
            func_as_string = "{0:#x}".format(func)
        else:
            func_as_string = func

        if level2_offset is not None and i == level2_offset:
            fp.write("\t/* Level 2 interrupts start here (offset: {}) */\n".
                     format(level2_offset))
        if level3_offset is not None and i == level3_offset:
            fp.write("\t/* Level 3 interrupts start here (offset: {}) */\n".
                     format(level3_offset))

        fp.write("\t{{(const void *){0}, (ISR){1}}},\n".format(param, func_as_string))
    fp.write("};\n")

def get_symbols(obj):
    for section in obj.iter_sections():
        if isinstance(section, SymbolTableSection):
            return {sym.name: sym.entry.st_value
                    for sym in section.iter_symbols()}

    error("Could not find symbol table")

def getindex(irq, irq_aggregator_pos):
    try:
        return irq_aggregator_pos.index(irq)
    except ValueError:
        error("IRQ {} not present in parent offsets ({}). ".
              format(irq, irq_aggregator_pos) +
              " Recheck interrupt configuration.")

def bit_mask(bits):
    mask = 0
    for _ in range(0, bits):
        mask = (mask << 1) | 1
    return mask

def update_masks():
    if sum(INTERRUPT_BITS) > 32:
        raise ValueError("Too many interrupt bits")

    INTERRUPT_LVL_BITMASK[0] = bit_mask(INTERRUPT_BITS[0])
    INTERRUPT_LVL_BITMASK[1] = bit_mask(INTERRUPT_BITS[1]) << INTERRUPT_BITS[0]
    INTERRUPT_LVL_BITMASK[2] = bit_mask(INTERRUPT_BITS[2]) << INTERRUPT_BITS[0] + INTERRUPT_BITS[1]

    debug("Level    Bits        Bitmask")
    debug("----------------------------")
    for i in range(3):
        bitmask_str = "0x" + format(INTERRUPT_LVL_BITMASK[i], '08X')
        debug(f"{i + 1:>5} {INTERRUPT_BITS[i]:>7} {bitmask_str:>14}")

def main():
    parse_args()

    with open(args.kernel, "rb") as fp:
        kernel = ELFFile(fp)
        syms = get_symbols(kernel)
        intlist = read_intlist(kernel, syms, args.intlist_section)

    if "CONFIG_MULTI_LEVEL_INTERRUPTS" in syms:
        max_irq_per = syms["CONFIG_MAX_IRQ_PER_AGGREGATOR"]

        INTERRUPT_BITS[0] = syms["CONFIG_1ST_LEVEL_INTERRUPT_BITS"]
        INTERRUPT_BITS[1] = syms["CONFIG_2ND_LEVEL_INTERRUPT_BITS"]
        INTERRUPT_BITS[2] = syms["CONFIG_3RD_LEVEL_INTERRUPT_BITS"]
        update_masks()

        if "CONFIG_2ND_LEVEL_INTERRUPTS" in syms:
            num_aggregators = syms["CONFIG_NUM_2ND_LEVEL_AGGREGATORS"]
            irq2_baseoffset = syms["CONFIG_2ND_LVL_ISR_TBL_OFFSET"]
            list_2nd_lvl_offsets = [syms['CONFIG_2ND_LVL_INTR_{}_OFFSET'.
                                         format(str(i).zfill(2))] for i in
                                    range(num_aggregators)]

            debug('2nd level offsets: {}'.format(list_2nd_lvl_offsets))

            if "CONFIG_3RD_LEVEL_INTERRUPTS" in syms:
                num_aggregators = syms["CONFIG_NUM_3RD_LEVEL_AGGREGATORS"]
                irq3_baseoffset = syms["CONFIG_3RD_LVL_ISR_TBL_OFFSET"]
                list_3rd_lvl_offsets = [syms['CONFIG_3RD_LVL_INTR_{}_OFFSET'.
                                             format(str(i).zfill(2))] for i in
                                        range(num_aggregators)]

                debug('3rd level offsets: {}'.format(list_3rd_lvl_offsets))

    nvec = intlist["num_vectors"]
    offset = intlist["offset"]

    if nvec > pow(2, 15):
        raise ValueError('nvec is too large, check endianness.')

    swt_spurious_handler = "((uintptr_t)&z_irq_spurious)"
    swt_shared_handler = "((uintptr_t)&z_shared_isr)"
    vt_spurious_handler = "z_irq_spurious"
    vt_irq_handler = "_isr_wrapper"

    debug('offset is ' + str(offset))
    debug('num_vectors is ' + str(nvec))

    # Set default entries in both tables
    if args.sw_isr_table:
        # All vectors just jump to the common vt_irq_handler. If some entries
        # are used for direct interrupts, they will be replaced later.
        if args.vector_table:
            vt = [vt_irq_handler for i in range(nvec)]
        else:
            vt = None
        # Default to spurious interrupt handler. Configured interrupts
        # will replace these entries.
        swt = [(0, swt_spurious_handler) for i in range(nvec)]
        shared = [([], 0) for i in range(nvec)]
    else:
        if args.vector_table:
            vt = [vt_spurious_handler for i in range(nvec)]
        else:
            error("one or both of -s or -V needs to be specified on command line")
        swt = None

    for irq, flags, func, param in intlist["interrupts"]:
        if flags & ISR_FLAG_DIRECT:
            if not vt:
                error("Direct Interrupt %d declared with parameter 0x%x "
                      "but no vector table in use"
                      % (irq, param))
            if param != 0:
                error("Direct irq %d declared, but has non-NULL parameter"
                        % irq)
            if not 0 <= irq - offset < len(vt):
                error("IRQ %d (offset=%d) exceeds the maximum of %d" %
                      (irq - offset, offset, len(vt) - 1))
            vt[irq - offset] = func
        else:
            # Regular interrupt
            if not swt:
                error("Regular Interrupt %d declared with parameter 0x%x "
                        "but no SW ISR_TABLE in use"
                        % (irq, param))

            if not "CONFIG_MULTI_LEVEL_INTERRUPTS" in syms:
                table_index = irq - offset
            else:
                # Figure out third level interrupt position
                debug('IRQ = ' + hex(irq))
                irq3 = (irq & INTERRUPT_LVL_BITMASK[2]) >> INTERRUPT_BITS[0] + INTERRUPT_BITS[1]
                irq2 = (irq & INTERRUPT_LVL_BITMASK[1]) >> INTERRUPT_BITS[0]
                irq1 = irq & INTERRUPT_LVL_BITMASK[0]

                if irq3:
                    irq_parent = irq2
                    list_index = getindex(irq_parent, list_3rd_lvl_offsets)
                    irq3_pos = irq3_baseoffset + max_irq_per*list_index + irq3 - 1
                    debug('IRQ_level = 3')
                    debug('IRQ_Indx = ' + str(irq3))
                    debug('IRQ_Pos  = ' + str(irq3_pos))
                    table_index = irq3_pos - offset

                # Figure out second level interrupt position
                elif irq2:
                    irq_parent = irq1
                    list_index = getindex(irq_parent, list_2nd_lvl_offsets)
                    irq2_pos = irq2_baseoffset + max_irq_per*list_index + irq2 - 1
                    debug('IRQ_level = 2')
                    debug('IRQ_Indx = ' + str(irq2))
                    debug('IRQ_Pos  = ' + str(irq2_pos))
                    table_index = irq2_pos - offset

                # Figure out first level interrupt position
                else:
                    debug('IRQ_level = 1')
                    debug('IRQ_Indx = ' + str(irq1))
                    debug('IRQ_Pos  = ' + str(irq1))
                    table_index = irq1 - offset

            if not 0 <= table_index < len(swt):
                error("IRQ %d (offset=%d) exceeds the maximum of %d" %
                      (table_index, offset, len(swt) - 1))
            if "CONFIG_SHARED_INTERRUPTS" in syms:
                if swt[table_index] != (0, swt_spurious_handler):
                    # check client limit
                    if syms["CONFIG_SHARED_IRQ_MAX_NUM_CLIENTS"] == shared[table_index][1]:
                        error(f"Reached shared interrupt client limit. Maybe increase"
                              + f" CONFIG_SHARED_IRQ_MAX_NUM_CLIENTS?")
                    lst = shared[table_index][0]
                    delta_size = 1
                    if not shared[table_index][1]:
                        lst.append(swt[table_index])
			# note: the argument will be fixed when writing the ISR table
			# to isr_table.c
                        swt[table_index] = (0, swt_shared_handler)
                        delta_size += 1
                    if (param, func) in lst:
                        error("Attempting to register the same ISR/arg pair twice.")
                    lst.append((param, func))
                    shared[table_index] = (lst, shared[table_index][1] + delta_size)
                else:
                    swt[table_index] = (param, func)
            else:
                if swt[table_index] != (0, swt_spurious_handler):
                    error(f"multiple registrations at table_index {table_index} for irq {irq} (0x{irq:x})"
                          + f"\nExisting handler 0x{swt[table_index][1]:x}, new handler 0x{func:x}"
                          + "\nHas IRQ_CONNECT or IRQ_DIRECT_CONNECT accidentally been invoked on the same irq multiple times?"
                    )
                else:
                    swt[table_index] = (param, func)

    with open(args.output_source, "w") as fp:
        write_source_file(fp, vt, swt, intlist, syms, shared)

if __name__ == "__main__":
    main()
