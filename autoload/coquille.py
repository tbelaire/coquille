import vim

import os
import re
import subprocess
import signal
import xml.etree.ElementTree as ET

from collections import deque

from coqtop import CoqTop

import vimbufsync
vimbufsync.check_version("0.1.0", who="coquille")

#: Pipe used to discuss with coqtop
coqtop = None

#: See vimbufsync ( https://github.com/def-lkb/vimbufsync )
saved_sync = None

#: Keeps track of what have been checked by Coq, and what is waiting to be
#: checked.
encountered_dots = []
send_queue = deque([])

error_at = None

logfile = open('/tmp/coqutille_log.txt', 'w')

def log(msg):
    logfile.write(str(msg) + "\n")
    logfile.flush()

###################
# synchronization #
###################

def sync():
    global saved_sync
    curr_sync = vimbufsync.sync()
    if not saved_sync or curr_sync.buf() != saved_sync.buf():
        _reset()
    else:
        (line, col) = saved_sync.pos()
        rewind_to(line - 1, col) # vim indexes from lines 1, coquille from 0
    saved_sync = curr_sync

def _reset():
    global saved_sync, encountered_dots, error_at, send_queue
    encountered_dots = []
    send_queue = deque([])
    saved_sync = None
    error_at   = None
    reset_color()

#####################
# exported commands #
#####################

def restart_coq(*args):
    global coqtop
    if coqtop: coqtop.close()
    try:
        coqtop = CoqTop(args, logfile)
    except OSError:
        print("Error: couldn't launch hoqtop")

def goto_last_sent_dot():
    (line, col) = (0,1) if encountered_dots == [] else encountered_dots[-1]
    vim.current.window.cursor = (line + 1, col)

def coq_rewind(steps=1):
    global encountered_dots

    if steps < 1 or encountered_dots == []:
        return

    if coqtop is None:
        print("Error: Coqtop isn't running. Are you sure you called :CoqLaunch?")
        return

    (messages, additional_steps) = coqtop.rewind(steps)

    if additional_steps is None:
        vim.command("call coquille#KillSession()")
        print('ERROR: the Coq process died')
        return

    nb_removed = steps + additional_steps
    encountered_dots = encountered_dots[:len(encountered_dots) - nb_removed]

    refresh()
    show_info("")

    # steps != 1 means that either the user called "CoqToCursor" or just started
    # editing in the "locked" zone. In both these cases we don't want to move
    # the cursor.
    if (steps == 1 and vim.eval('g:coquille_auto_move') == 'true'):
        goto_last_sent_dot()

def coq_to_cursor():
    if coqtop is None:
        print("Error: Coqtop isn't running. Are you sure you called :CoqLaunch?")
        return

    sync()

    (cline, ccol) = vim.current.window.cursor
    (line, col)  = encountered_dots[-1] if encountered_dots else (0,0)

    if cline < line or (cline == line and ccol < col):
        rewind_to(cline - 1, ccol)
    else:
        while True:
            r = _get_message_range((line, col))
            if r is not None and r['stop'] <= (cline - 1, ccol):
                line = r['stop'][0]
                col  = r['stop'][1] + 1
                send_queue.append(r)
            else:
                break

        send_until_fail()

def coq_next():
    if coqtop is None:
        print("Error: Coqtop isn't running. Are you sure you called :CoqLaunch?")
        return

    sync()

    (line, col)  = encountered_dots[-1] if encountered_dots else (0,0)
    message_range = _get_message_range((line, col))

    if message_range is None: return

    send_queue.append(message_range)

    send_until_fail()

    if (vim.eval('g:coquille_auto_move') == 'true'):
        goto_last_sent_dot()

def coq_raw_query(*args):
    # log("Starting query with args %s" %(args))
    if coqtop is None:
        log("Error: Coqtop isn't running. Are you sure you called :CoqLaunch?")
        return

    raw_query = ' '.join(args)

    encoding = vim.eval("&encoding")

    log("About to send cmd")
    (messages, response) = coqtop.interp(raw_query.decode(encoding), raw=True)
    handle_messages(messages)
    if response is None:
        vim.command("call coquille#KillSession()")
        print('ERROR: the Coq process died')
        return
    # Doesn't even matter what response is, if it's failure,
    # that's a message.

def launch_coq(*args):
    restart_coq(*args)

def debug():
    if encountered_dots:
        print("encountered dots = [")
        for (line, col) in encountered_dots:
            print("  (%d, %d) ; " % (line, col))
        print("]")

#####################################
# IDE tools: Goal, Infos and colors #
#####################################

def refresh():
    show_goal()
    reset_color()

def show_goal():
    buff = None
    for b in vim.buffers:
        if re.match(".*Goals$", b.name):
            buff = b
            break
    del buff[:]

    (messages, goals) = coqtop.goals()

    if goals is None:
        return

    plural_opt = '' if len(goals) == 1 else 's'
    buff.append(['%d subgoal%s' % (len(goals), plural_opt), ''])

    for idx, goal in enumerate(goals):
        if idx == 0:
            # we print the environment only for the current subgoal
            for hyp in goal.hypothesis:
                buff.append(hyp.split('\n'))
        buff.append('')
        buff.append('======================== ( %d / %d )' % (idx+1 , len(goals)))
        buff.append(goal.conclusion.split("\n"))
        buff.append('')

def show_info(info_msg):
    buff = None
    for b in vim.buffers:
        if re.match(".*Infos$", b.name):
            buff = b
            break

    del buff[:]
    if info_msg is not None:
        lst = info_msg.split('\n')
        buff.append(lst)

def handle_messages(messages):
    new_info_msg = ""
    for message in messages:
        level, info = message
        if info:
            new_info_msg += info
            new_info_msg += "\n\n"

    # TODO if we want persistant messages do this
    # otherwise unconditionally show the new message
    if len(new_info_msg) > 0:
        show_info(new_info_msg)

def reset_color():
    global error_at
    # Clear current coloring (dirty)
    if int(vim.eval('b:checked')) != -1:
        vim.command('call matchdelete(b:checked)')
        vim.command('let b:checked = -1')
    if int(vim.eval('b:sent')) != -1:
        vim.command('call matchdelete(b:sent)')
        vim.command('let b:sent = -1')
    if int(vim.eval('b:errors')) != -1:
        vim.command('call matchdelete(b:errors)')
        vim.command('let b:errors = -1')
    # Recolor
    if encountered_dots:
        (line, col) = encountered_dots[-1]
        start = { 'line': 0 , 'col': 0 }
        stop  = { 'line': line + 1, 'col': col }
        zone = _make_matcher(start, stop)
        vim.command("let b:checked = matchadd('CheckedByCoq', '%s')" % zone)
    if len(send_queue) > 0:
        (l, c) = encountered_dots[-1] if encountered_dots else (0,-1)
        r = send_queue.pop()
        send_queue.append(r)
        (line, col) = r['stop']
        start = { 'line': l , 'col': c + 1 }
        stop  = { 'line': line + 1, 'col': col }
        zone = _make_matcher(start, stop)
        vim.command("let b:sent = matchadd('SentToCoq', '%s')" % zone)
    if error_at:
        ((sline, scol), (eline, ecol)) = error_at
        start = { 'line': sline + 1, 'col': scol }
        stop  = { 'line': eline + 1, 'col': ecol }
        zone = _make_matcher(start, stop)
        vim.command("let b:errors = matchadd('CoqError', '%s')" % zone)
        error_at = None

def rewind_to(line, col):
    if coqtop is None:
        print('Internal error: vimbufsync is still being called but coqtop\
                appears to be down.')
        print('Please report.')
        return

    predicate = lambda x: x <= (line, col)
    lst = filter(predicate, encountered_dots)
    steps = len(encountered_dots) - len(lst)
    coq_rewind(steps)

#############################
# Communication with Coqtop #
#############################

def send_until_fail():
    """
    Tries to send every message in [send_queue] to Coq, stops at the first
    error.
    When this function returns, [send_queue] is empty.
    """
    global encountered_dots, error_at

    encoding = vim.eval('&fileencoding') or "utf-8"

    all_messages = []
    while len(send_queue) > 0:
        reset_color()
        vim.command('redraw')

        command_range = send_queue.popleft()
        command = _between(command_range['start'], command_range['stop'])
        command = command.decode(encoding)
        (messages, response) = coqtop.interp(command)
        all_messages += messages

        if response is None:
            vim.command("call coquille#KillSession()")
            print('ERROR: the Coq process died')
            handle_messages(all_messages)
            return
        (ok, err) = response
        if ok:
            (eline, ecol) = command_range['stop']
            encountered_dots.append((eline, ecol + 1))
        else:
            send_queue.clear()
            loc_s, loc_e = err
            (l, c) = command_range['start']
            (l_start, c_start) = _pos_from_offset(c, command, loc_s)
            (l_stop, c_stop)   = _pos_from_offset(c, command, loc_e)
            error_at = ((l + l_start, c_start), (l + l_stop, c_stop))

    handle_messages(all_messages)
    refresh()

def _pos_from_offset(col, msg, offset):
    str = msg[:offset]
    lst = str.split('\n')
    line = len(lst) - 1
    col = len(lst[-1]) + (col if line == 0 else 0)
    return (line, col)

#################
# Miscellaneous #
#################
def _parse_xml_list(s):
    # s may be <msg>1</msg><msg>2</msg>, which fails to parse
    surround = "<special_list>%s</special_list>" % s
    # Now we have a proper xml tree, not forest
    try:
        elts = ET.fromstring(surround)
        return list(elts)
    except ET.ParseError:
        return None



def _between(begin, end):
    """
    Returns a string corresponding to the portion of the buffer between the
    [begin] and [end] positions.
    """
    (bline, bcol) = begin
    (eline, ecol) = end
    buf = vim.current.buffer
    acc = ""
    for line, str in enumerate(buf[bline:eline + 1]):
        start = bcol if line == 0 else 0
        stop  = ecol + 1 if line == eline - bline else len(str)
        acc += str[start:stop] + '\n'
    return acc

def _get_message_range(after):
    """ See [_find_next_chunk] """
    (line, col) = after
    end_pos = _find_next_chunk(line, col)
    return { 'start':after , 'stop':end_pos } if end_pos is not None else None

def _find_next_chunk(line, col):
    """
    Returns the position of the next chunk dot after a certain position.
    That can either be a bullet if we are in a proof, or "a string" terminated
    by a dot (outside of a comment, and not denoting a path).
    """
    buff = vim.current.buffer
    blen = len(buff)
    bullets = ['{', '}', '-', '+', '*']
    # We start by striping all whitespaces (including \n) from the beginning of
    # the chunk.
    while line < blen and buff[line][col:].strip() == '':
        line += 1
        col = 0

    if line >= blen: return

    while buff[line][col] == ' ': # FIXME: keeping the stripped line would be
        col += 1                  #   more efficient.

    # Then we check if the first character of the chunk is a bullet.
    # Intially I did that only when I was sure to be in a proof (by looking in
    # [encountered_dots] whether I was after a "collapsable" chunk or not), but
    #   1/ that didn't play well with coq_to_cursor (as the "collapsable chunk"
    #      might not have been sent/detected yet).
    #   2/ The bullet chars can never be used at the *beginning* of a chunk
    #      outside of a proof. So the check was unecessary.
    if buff[line][col] in bullets:
        return (line, col + 1)

    # We might have a commentary before the bullet, we should be skiping it and
    # keep on looking.
    tail_len = len(buff[line]) - col
    if (tail_len - 1 > 0) and buff[line][col] == '(' and buff[line][col + 1] == '*':
        com_end = _skip_comment(line, col + 2, 1)
        if not com_end: return
        (line, col) = com_end
        return _find_next_chunk(line, col)


    # If the chunk doesn't start with a bullet, we look for a dot.
    return _find_dot_after(line, col)

def _find_dot_after(line, col):
    """
    Returns the position of the next "valid" dot after a certain position.
    Valid here means: recognized by Coq as terminating an input, so dots in
    comments, strings or ident paths are not valid.
    """
    b = vim.current.buffer
    if line >= len(b): return
    s = b[line][col:]
    dot_pos = s.find('.')
    com_pos = s.find('(*')
    str_pos = s.find('"')
    if com_pos == -1 and dot_pos == -1 and str_pos == -1:
        # Nothing on this line
        return _find_dot_after(line + 1, 0)
    elif dot_pos == -1 or (com_pos > - 1 and dot_pos > com_pos) or (str_pos > - 1 and dot_pos > str_pos):
        if str_pos == -1 or (com_pos > -1 and str_pos > com_pos):
            # We see a comment opening before the next dot
            com_end = _skip_comment(line, com_pos + 2 + col, 1)
            if not com_end: return
            (line, col) = com_end
            return _find_dot_after(line, col)
        else:
            # We see a string starting before the next dot
            str_end = _skip_str(line, str_pos + col + 1)
            if not str_end: return
            (line, col) = str_end
            return _find_dot_after(line, col)
    elif dot_pos < len(s) - 1 and s[dot_pos + 1] != ' ':
        # Sometimes dot are used to access module fields, we don't want to stop
        # just after the module name.
        # Example: [Require Import Coq.Arith]
        return _find_dot_after(line, col + dot_pos + 1)
    elif dot_pos + col > 0 and b[line][col + dot_pos - 1] == '.':
        # FIXME? There might be a cleaner way to express this.
        # We don't want to capture ".."
        if dot_pos + col > 1 and b[line][col + dot_pos - 2] == '.':
            # But we want to capture "..."
            return (line, dot_pos + col)
        else:
            return _find_dot_after(line, col + dot_pos + 1)
    else:
        return (line, dot_pos + col)

# TODO? factorize [_skip_str] and [_skip_comment]
def _skip_str(line, col):
    """
    Used when we encountered the start of a string before a valid dot (see
    [_find_dot_after]).
    Returns the position of the end of the string.
    """
    b = vim.current.buffer
    if line >= len(b): return
    s = b[line][col:]
    str_end = s.find('"')
    if str_end > -1:
        return (line, col + str_end + 1)
    else:
        return _skip_str(line + 1, 0)

def _skip_comment(line, col, nb_left):
    """
    Used when we encountered the start of a comment before a valid dot (see
    [_find_dot_after]).
    Returns the position of the end of the comment.
    """
    if nb_left == 0:
        return (line, col)

    b = vim.current.buffer
    if line >= len(b): return
    s = b[line][col:]
    com_start = s.find('(*')
    com_end = s.find('*)')
    if com_end > -1 and (com_end < com_start or com_start == -1):
        return _skip_comment(line, col + com_end + 2, nb_left - 1)
    elif com_start > -1:
        return _skip_comment(line, col + com_start + 2, nb_left + 1)
    else:
        return _skip_comment(line + 1, 0, nb_left)

def _will_be_collapsed(s):
    """
    Collapsable part are useful when we want to rewind to a certain position.
    Indeed when we send something to Coq, a "step" is just a string between two
    "valid" dots, but when we rewind a step might be /bigger/ than that as Coq
    doesn't rewind just a "Qed" or "Defined" when it meets one, but the whole
    proof (returning the "actual" number of steps rewinded).
    We could just rewind one step at a time until we reach the desired point in
    the buffer, but this seems more efficient.
    """
    if re.match(".*(Theorem|Goal|Lemma|Next Obligation).*", s):
        return True
    elif re.match('.*Definition .*', s) and not re.search(':=', s):
        return True
    else:
        return False

def _time_to_collapse(s):
    """ Used in conjunction with [_will_be_collapsed] """
    return True if re.match('.*(Qed|Defined)\.$', s) else False

## I thought python was the language with a big stdlib...
def rfind(lst, cond):
    tmp = None
    for idx, elt in enumerate(lst):
        if cond(elt): tmp = idx
    return tmp

################################################
# The ugly through behind regions highlighting #
################################################

def _make_matcher(start, stop):
    if start['line'] == stop['line']:
        return _easy_matcher(start, stop)
    else:
        return _hard_matcher(start, stop)

def _easy_matcher(start, stop):
    startl = ""
    startc = ""
    if start['line'] > 0:
        startl = "\%>{0}l".format(start['line'] - 1)
    if start['col'] > 0:
        startc = "\%>{0}c".format(start['col'])
    return '{0}{1}\%<{2}l\%<{3}c'.format(startl, startc, stop['line'] + 1, stop['col'] + 1)

def _hard_matcher(start, stop):
    first_start = {'line' : start['line'], 'col' : start['col']}
    first_stop =  {'line' : start['line'], 'col' : 4242}
    first_line = _easy_matcher(first_start, first_stop)
    mid_start = {'line' : start['line']+1, 'col' : 0}
    mid_stop =  {'line' : stop['line']-1 , 'col' : 4242}
    middle = _easy_matcher(mid_start, mid_stop)
    last_start = {'line' : stop['line'], 'col' : 0}
    last_stop =  {'line' : stop['line'], 'col' : stop['col']}
    last_line = _easy_matcher(last_start, last_stop)
    return "{0}\|{1}\|{2}".format(first_line, middle, last_line)
