
import signal
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

try:
    import Queue
except ImportError:
    import queue as Queue

import xml_stream_parser
from async_pipe import AsyncPipe

from collections import namedtuple

# Goal should have utf-8 encoded values
Goal = namedtuple("Goal", ['identifier', 'hypothesis', 'conclusion'])

# Maximum time to wait between coq responses
TIMEOUT = 3.0
# Under 0.5s is unreliable

class CoqTop (object):
    def __init__(self,
                 coqtop_path,
                 args,
                 logfile,
                 debug=False,
                 xml_parser=None):

        xml_parser = (xml_parser or
                      xml_stream_parser.enqueue_xml_stream)
        # Other options are enqueue_xml, enqueue_xml_one_by_one

        def ignore_sigint():
            signal.signal(signal.SIGINT, signal.SIG_IGN)

        self.coqtop = AsyncPipe(
            dict(
                args=[coqtop_path, "-ideslave", "-debug"] + list(args),  #TODO debug flag
                stderr=logfile,
                preexec_fn=ignore_sigint),
            parser=xml_parser)
        # TODO Windows support by passing in
        # xml_stream_parser.enqueue_xml_one_by_one
        self.logfile = logfile

    def close(self):
        try:
            # TODO this crashes with error about closing coqtop.stdout while
            # it's being used in another thread
            self.coqtop.close()
        except OSError:
            pass

    # Low level communication

    def send_cmd(self, xml_tree, encoding='utf-8'):
        serialized = ET.tostring(xml_tree, encoding)
        # log("TO coq: %s" % serialized)
        self.coqtop.write(serialized)

    def send_text(self, string):
        # log("TO coq: %s" % string)
        self.coqtop.write("%s\n" % string)

    def get_answer(self):
        messages = []
        while True:
            try:
                response = self.coqtop.get(True, TIMEOUT)
                if response.tag == "message":
                    message = CoqTop._parse_message(response)
                    if message is not None:
                        messages.append(message)
                    else:
                        self.logfile.write("Dropping unparsed message: {}\n"
                                      .format(ET.tostring(response)))
                elif response.tag == "value":
                    return (messages, response)
                else:
                    self.logfile.write("Unknown xml response: {}\n".format(
                        ET.tostring(response)))
            except Queue.Empty:
                return (messages, None)

    # Smart commands
    # All return (messages, response)
    # if the request timed out, response is None
    # Otherwise it depends

    def rewind(self, steps):
        x = int(steps) # Check that steps is integral
        self.send_text('<call id="1" val="rewind" steps="{}"></call>'
                       .format(x))
        (messages, response) = self.get_answer()
        if response.get('val') == 'good':
            int_container = response.find('int')
            if int_container is not None:
                # TODO error handling
                return (messages, int(int_container.text))
        return (messages, None)


    def interp(self, message, raw=False):
        """ Returns (messages, (ok, extra_data))

        extra_data is the pair of start and end of the string that
        caused the error.
        messages will have the reason for failing included in it,
        at level 'error'.
        """
        if not raw:
            self.send_text('<call id="1" val="interp">{}</call>'
                             .format(escape(message)))
        else:
            self.send_text('<call id="1" raw="true" val="interp">{}</call>'
                             .format(escape(message)))
        (messages, response) = self.get_answer()
        # I'm tired of being nagged
        messages = [(level, text) for (level, text) in messages
                    if text !=
                    "Query commands should not be inserted in scripts"]

        if response is None:
            return (messages, None)
        if response.get('val') == 'good':
            return (messages, (True, None))
        elif response.get('val') == 'fail':
            fail_msg = ('error', response.text)
            messages.append(fail_msg)
            return (messages,
                    (False,
                     (int(response.get('loc_s')),
                      int(response.get('loc_e')))))
        elif response.get('val') == 'unsafe':
            # This means we used Admited or friends.
            # Just make sure the editor highlights it ok.
            return (messages, (True, 'Unsafe'))
        else:
            print("(ANOMALY) unknown answer: %s" % ET.tostring(response))

    def goals(self):
        self.send_text('<call id="1" val="goal"></call>')
        (messages, response) = self.get_answer()
        (ok, goals) = CoqTop._parse_goals(response)
        if ok:
            return (messages, goals)
        else:
            # TODO handle better
            return (messages, goals)


    # XML parsers
    @staticmethod
    def _parse_goals(resp):
        # <goals><list>
        #  <goal>
        #    <string>3</string>
        #    <list>
        #      <string>a : Type</string>
        #    </list>
        #    <string>true = true</string>
        #  </goal>
        # </list><list /></goals>

        # option of [
        #   list of goal;  Foreground goals
        #   list of pair of (list of goal) (list of goal); 
        #    # Background Goals zipper
        # ]

        if resp is None:
            return (False, "Invalid response (None)")
        val = resp.get('val', None)
        if val != "good":
            return (False, "Bad request")
        # Expect to get an option
        option = resp.find('option')
        if option is None:
            return (False, "Bad request (no option)")
        if option.get('val', None) != "some":
            return (False, None)
        try:
            goals = option.find('goals')
            # TODO error handling

            [fg_goals, bg_goals] = list(goals)
            parsed_goals = []
            for goal in fg_goals:
                # (** Unique goal identifier *)
                # (** List of hypotheses *)
                # (** Goal conclusion *)
                [id, hyps, con] = list(goal)
                assert(id.tag == 'string')
                assert(hyps.tag == 'list')
                assert(con.tag == 'string')

                parsed_goals.append(Goal(id.text, [t.text for t in hyps], con.text))

            return (True, parsed_goals)
        except (ValueError, AssertionError):
            return (False, "Failed to parse")
        # TODO handle background goals?

    @staticmethod
    def _parse_message(message):
        """Parse <messages> into (level, message) pairs"""
        try:
            level, string = list(message)
            assert(level.tag == "message_level")
            assert(string.tag == "string")
            return (level.get("val"), string.text)
        except (ValueError, AssertionError):
            return None




if __name__ == "__main__":
    import sys
    import time
    coqtop = CoqTop("hoqtop", [], debug=True, logfile=sys.stdout)

    print( coqtop.interp('Require Import Overture.') )
    print( coqtop.interp('Require Import HoTT.types.Bool.') )
    print( coqtop.interp('Check true.') )

    # Rewind test
    print( coqtop.rewind(1) )

    print( coqtop.interp('Check true.', raw=True) )

    print(coqtop.goals())

    print( coqtop.interp('Goal forall A:Type, true = true.') )
    print( coqtop.interp('Proof.') )
    print( coqtop.interp('intros a.') )

    print(coqtop.goals())

    print( coqtop.interp('Qed.') )

    print( coqtop.interp('Admitted.') )
    print( coqtop.rewind(1) )


