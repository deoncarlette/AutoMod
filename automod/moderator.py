"""
moderator.py
"""
import logging
import threading
import time

from datetime import datetime

import pytz

from .clubhouse import Config
from .clubhouse import Clubhouse
from .tracker import Tracker

set_interval = Clubhouse.set_interval


# noinspection DuplicatedCode
class ModClient(Clubhouse, Tracker):
    # Should I add phone number and verification code to __init__?
    # Add pickling to save data in case client refreshes before channel ends
    def __init__(self):
        super().__init__()


    def __str__(self):
        pass

    def channel_init(
            self, channel, api_retry_interval_sec=10, thread_timeout=120,
            announcement=None, announcement_interval_min=60, announcement_delay=None):

        join_info = self.set_join_status(channel)
        channel_info, users_info, client_info = self.set_channel_status(channel)

        self.set_channel_init()

        self.keep_alive_thread = self.keep_alive_ping(channel)

        if self.chat_enabled:
            targeted_message = self.set_targeted_message()
            hello_message = self.set_hello_message(targeted_message)
            self.send_room_chat(channel, hello_message, delay=5)

        if self.waiting_speaker:
            is_speaker = self.wait_to_speak(channel, api_retry_interval_sec, thread_timeout)

            if not is_speaker:
                self.terminate_channel(channel)
                return

        if self.waiting_mod:
            is_mod = self.wait_for_mod(channel, api_retry_interval_sec, thread_timeout)

            if not is_mod:
                self.terminate_channel(channel)
                return

        if self.chat_enabled:
            if self.url_announcement:
                self.url_announcement_thread = self.set_url_announcement(channel)

            self.runtime_announcement_thread = self.set_runtime_announcement(channel)

            if announcement:
                self.announcement_thread = self.set_announcement(
                    channel, announcement, announcement_interval_min, announcement_delay)

        if self.channel_type == "public":
            self.data_dump(join_info, "join")
            self.data_dump(channel_info, "channel")

        return join_info, channel_info, users_info, client_info

    def get_join_info(self, channel):
        join_info = self.channel.join_channel(channel)
        return join_info

    def set_join_status(self, channel):
        join_info = self.get_join_info(channel)
        self.channel_type = self.get_channel_type(join_info)
        self.host_name = self.get_host_name(join_info)
        self.club_id = self.get_club(join_info)
        self.auto_speaker_approval = self.get_auto_speaker_approval(join_info)
        self.time_created = self.get_time_created(join_info)
        self.token = self.get_token(join_info)
        self.screened_user_set = self.get_users_in_room(join_info)
        self.chat_enabled = self.get_chat_enabled(join_info)

        return join_info

    def get_channel_info(self, channel):
        channel_info = self.channel.get_channel(channel)
        return channel_info

    def set_channel_status(self, channel):
        channel_info = self.get_channel_info(channel)
        users_info = self.get_users_info(channel_info, channel_info=True)
        client_info = self.get_client_info(users_info, user_info=True)

        self.chat_enabled = self.get_chat_enabled(channel_info)
        self.filtered_users_list = self.filter_screened_users(users_info)
        # self.screened_user_set = self.update_screened_users()
        self.active_speaker = self.get_speaker_status(client_info, client_info=True)
        self.active_mod = self.get_mod_status(client_info, client_info=True)

        return channel_info, users_info, client_info

    def set_channel_init(self):

        # Set waiting_mod and waiting_speaker statuses
        if (self.channel_type == "private" or self.channel_type == "public") and not self.active_speaker:
            self.waiting_mod = True
            self.waiting_speaker = True

        elif not self.active_mod:
            self.waiting_mod = True
            self.waiting_speaker = False

        # Set url_announcement status
        if self.channel_type == "private":
            self.url_announcement = True

        # Set in_automod_club status
        if self.club_id in self.automod_clubs:
            self.in_automod_club = True

        # Set in_social_club status
        if self.club_id in self.social_clubs:
            self.in_social_club = True

        return True

    @set_interval(15)
    def run_active_channel(
            self, channel, message_delay=2, reconnect_interval=10, reconnect_timeout=120,
            dump_interval=8):

        channel_info, users_info, client_info = self.refresh_channel_status(channel)

        if not self.active_channel:
            is_active = self.wait_for_reconnection(channel, reconnect_interval, reconnect_timeout)

            if not is_active:
                self.terminate_channel(channel)
                return

        if self.granted_speaker and not self.active_speaker:
            is_speaker = self.wait_to_speak(channel, reconnect_interval, reconnect_timeout)

            if not is_speaker:
                self.terminate_channel(channel)
                return

        if self.granted_mod and not self.active_mod:
            is_mod = self.wait_to_speak(channel, reconnect_interval, reconnect_timeout)

            if not is_mod:
                self.terminate_channel(channel)
                return

        if self.channel_type != "public":
            self.welcome_guests(channel, users_info, message_delay)

        self.invite_guests(channel, users_info, message_delay)
        self.mod_guests(channel, users_info, message_delay)

        if self.s3_dump_counter == dump_interval:
            self.s3_dump_counter = 0

            feed_info = self.client.feed()
            self.data_dump(feed_info, "feed")

            if self.channel_type == "public":
                self.data_dump(channel_info, "channel")

        self.s3_dump_counter += 1

        return True

    @set_interval(30)
    def keep_alive_ping(self, channel):
        self.channel.active_ping(channel)
        return True

    def refresh_channel_status(self, channel):
        channel_info = self.get_channel_info(channel)
        if not channel_info:
            self.active_channel = False
            return None, None, None

        users_info = self.get_users_info(channel_info, channel_info=True)
        client_info = self.get_client_info(users_info, user_info=True)

        self.chat_enabled = self.get_chat_enabled(channel_info)
        self.active_speaker = self.get_speaker_status(client_info, client_info=True)
        self.active_mod = self.get_mod_status(client_info, client_info=True)

        return channel_info, users_info, client_info

    def wait_for_reconnection(self, channel, interval=10, timeout=120):

        active_channel_status = threading.Event()
        self.waiting_reconnect_thread = threading.Thread(
            target=self.recheck_speaker_status, args=(channel, interval, active_channel_status))

        self.waiting_reconnect_thread.daemon = True
        self.waiting_reconnect_thread.start()
        logging.info(f"Stopped: {self.waiting_reconnect_thread}")
        self.waiting_speaker_thread.join(timeout)
        logging.info(f"Joined: {self.waiting_reconnect_thread}")

        return True if self.active_channel else False

    def recheck_connection_status(self, channel, interval, active_channel_status):

        while not active_channel_status.isSet():
            join = self.channel.join_channel(channel)
            if join:
                self.active_channel = True

            if self.active_channel:
                active_channel_status.set()
                logging.info(f"Stopped: {self.waiting_reconnect_thread}")
            else:
                logging.info("Still attempting to reconnect")

            active_channel_status.wait(interval)

    def get_users_info(self, param, channel_info=False):

        if channel_info:
            user_info = param.get("users")

        else:
            channel_info = self.get_channel_info(param)
            user_info = channel_info.get("users")

        return user_info

    def get_users_in_room(self, join_info):
        users = join_info.get("users")
        users_set = set(_.get("user_id") for _ in users)
        users_set.add(self.client_id)
        return users_set

    def filter_screened_users(self, user_info, for_speaker=False, for_mod=False):

        if for_speaker:
            unscreened_new_users_list = [_ for _ in user_info if _.get("user_id") not in self.screened_for_speaker_set]

        elif for_mod:
            unscreened_new_users_list = [_ for _ in user_info if _.get("user_id") not in self.screened_for_mod_set]

        else:
            unscreened_new_users_list = [_ for _ in user_info if _.get("user_id") not in self.screened_user_set]

        return unscreened_new_users_list

    def update_screened_users(self):
        union = self.screened_user_set.union(self.unscreened_user_set)
        return union

    def get_client_info(self, param, channel_info=False, user_info=False):

        if user_info:
            client_info = [_ for _ in param if _.get("user_id") == self.client_id][0]

        elif channel_info:
            user_info = param.pop("users")
            client_info = [_ for _ in user_info if _.get("user_id") == self.client_id][0]

        else:
            channel_info = self.get_channel_info(param)
            user_info = channel_info.pop("users")
            client_info = [_ for _ in user_info if _.get("user_id") == self.client_id][0]

        return client_info

    def get_speaker_status(self, param, channel_info=False, user_info=False, client_info=False):

        if client_info:
            speaker_status = param.get("is_speaker")

        elif user_info:
            client_info = [_ for _ in param if _.get("user_id") == self.client_id][0]
            speaker_status = client_info.get("is_speaker")

        elif channel_info:
            user_info = param.pop("users")
            client_info = [_ for _ in user_info if _.get("user_id") == self.client_id][0]
            speaker_status = client_info.get("is_speaker")

        else:
            channel_info = self.get_channel_info(param)
            user_info = channel_info.pop("users")
            client_info = [_ for _ in user_info if _.get("user_id") == self.client_id][0]
            speaker_status = client_info.get("is_speaker")

        return speaker_status

    def request_to_speak(self, channel):
        request = self.channel.audience_reply(channel)
        return request

    def wait_to_speak(self, channel, interval=10, timeout=120):

        self.request_to_speak(channel)
        active_speaker_status = threading.Event()
        self.waiting_speaker_thread = threading.Thread(
            target=self.recheck_speaker_status, args=(channel, interval, active_speaker_status))

        self.waiting_speaker_thread.daemon = True
        self.waiting_speaker_thread.start()
        logging.info(f"Stopped: {self.waiting_speaker_thread}")
        self.waiting_speaker_thread.join(timeout)
        logging.info(f"Joined: {self.waiting_speaker_thread}")

        return True if self.active_speaker else False

    def recheck_speaker_status(self, channel, interval, active_speaker_status):

        while not active_speaker_status.isSet():
            self.channel.accept_speaker_invite(channel, self.client_id)
            self.granted_speaker = self.get_speaker_status(channel)

            if self.granted_speaker:
                active_speaker_status.set()
                self.waiting_speaker = False
                self.active_speaker = True
                logging.info(f"Stopped: {self.waiting_speaker_thread}")

            else:
                logging.info("Still waiting to join stage")

            active_speaker_status.wait(interval)

    def get_mod_status(self, param, channel_info=False, user_info=False, client_info=False):

        if client_info:
            mod_status = param.get("is_moderator")

        elif user_info:
            client_info = [_ for _ in param if _.get("user_id") == self.client_id][0]
            mod_status = client_info.get("is_moderator")

        elif channel_info:
            user_info = param.pop("users")
            client_info = [_ for _ in user_info if _.get("user_id") == self.client_id][0]
            mod_status = client_info.get("is_moderator")

        else:
            channel_info = self.get_channel_info(param)
            user_info = channel_info.pop("users")
            client_info = [_ for _ in user_info if _.get("user_id") == self.client_id][0]
            mod_status = client_info.get("is_moderator")

        return mod_status

    def wait_for_mod(self, channel, interval=10, timeout=120):

        active_mod_status = threading.Event()
        self.waiting_mod_thread = threading.Thread(
            target=self.recheck_mod_status, args=(channel, interval, active_mod_status))

        self.waiting_mod_thread.daemon = True
        self.waiting_mod_thread.start()
        logging.info(f"Started: {self.waiting_mod_thread}")
        self.waiting_mod_thread.join(timeout)
        logging.info(f"Joined: {self.waiting_mod_thread}")

        return True if self.active_mod else False

    def recheck_mod_status(self, channel, interval, active_mod_status):

        while not active_mod_status.isSet():
            self.granted_mod = self.get_mod_status(channel)

            if self.granted_mod:
                active_mod_status.set()
                self.waiting_mod = False
                self.active_mod = True
                logging.info(f"Stopped: {self.waiting_mod_thread}")

            else:
                logging.info("Still waiting mod privileges")

            active_mod_status.wait(interval)

    @staticmethod
    def get_chat_enabled(join_or_channel_info):
        chat_enabled = join_or_channel_info.get("is_chat_enabled")
        logging.info(chat_enabled)
        return chat_enabled

    @staticmethod
    def get_host_name(join_info):
        creator_id = join_info.get("creator_user_profile_id")
        host_name = [_.get("first_name") for _ in join_info.get("users") if _.get("user_id") == creator_id]

        if host_name:
            host_name = host_name[0]

        else:
            host_name = join_info.get("users")[0].get("first_name")

        logging.info(host_name)
        return host_name

    def set_hello_message(self, targeted_message=None):
        host_name = self.host_info.get("name")
        message = f"🤖 Hello {host_name}! I'm AutoMod! 🎉 "


        if isinstance(targeted_message, str):
            message = [message + targeted_message]

        elif isinstance(targeted_message, list):
            message = [message] + targeted_message

        return message

    def set_targeted_message(self):

        if self.waiting_speaker and self.waiting_mod:
            targeted_message = self.request_speak_and_mod_message()

        elif self.waiting_mod:
            targeted_message = self.request_mod_message()

        elif self.waiting_speaker:
            targeted_message = self.request_speak_message()

        else:
            targeted_message = None

        return targeted_message

    def send_room_chat(self, channel, message, delay=10):
        response = False

        if isinstance(message, str):
            message = [message]

        for _ in message:
            run = self.chat.send_chat(channel, _)
            response = run.get("success")
            time.sleep(delay)

        return response

    @staticmethod
    def request_speak_and_mod_message():
        message = "If you'd like to use my features, please invite me to speak and make me a Moderator. ✳️"
        return message

    @staticmethod
    def request_mod_message():
        message = "If you'd like to use my features, please make me a Moderator. ✳️"
        return message

    @staticmethod
    def request_speak_message():
        message = "If you'd like to hear music, please invite me to speak. 🎶"
        return message

    @staticmethod
    def set_welcome_message(first_name, user_id):
        name = first_name
        message = f"Welcome {name}! 🎉"

        if user_id == 2350087:
            message = f"Welcome Disco Doggie! 🎉"

        elif user_id == 1414736198:
            message = "Tabi! Hello my love! 😍"

        elif user_id == 47107:
            message_2 = "Ryan, please don't choose violence today!"
            message = [message, message_2]

        elif user_id == 2247221:
            message_2 = "First"
            message_3 = "And furthermore, infinitesimal"
            message = [message, message_2, message_3]

        return message

    def welcome_guests(self, channel, user_info, message_delay=2):

        for user in user_info:
            user_id = user.get("user_id")
            first_name = user.get("first_name")
            welcome_message = self.set_welcome_message(first_name, user_id)

            if user_id not in self.already_welcomed_set and user_id not in self.screened_user_set:
                self.send_room_chat(channel, welcome_message, message_delay)
                self.already_welcomed_set.add(user_id)

    def invite_guests(self, channel, user_info, message_delay=2):

        if self.in_automod_club or self.in_social_club:

            for user in user_info:
                user_id = user.get("user_id")
                first_name = user.get("first_name")
                is_speaker = user.get("is_speaker")
                is_invited = user.get("is_invited_as_speaker")
                welcome_message = self.set_welcome_message(first_name, user_id)

                if not is_speaker and not is_invited:
                    self.mod.invite_speaker(channel, user_id)
                    self.send_room_chat(channel, welcome_message, message_delay)
                    self.already_welcomed_set.add(user_id)

            return True

        filtered_users = self.filter_screened_users(user_info, for_speaker=True)

        for user in filtered_users:
            user_id = user.get("user_id")

            if user_id in self.guest_list:
                first_name = user.get("first_name")
                is_speaker = user.get("is_speaker")
                is_invited = user.get("is_invited_as_speaker")
                welcome_message = self.set_welcome_message(first_name, user_id)

                if not is_speaker and not is_invited:
                    self.mod.invite_speaker(channel, user_id)
                    run = self.send_room_chat(channel, welcome_message, message_delay)
                    if run:
                        self.already_welcomed_set.add(user_id)

            self.screened_for_speaker_set.add(user_id)

    def mod_guests(self, channel, user_info, message_delay=10):

        if self.in_social_club:

            for user in user_info:
                user_id = user.get("user_id")
                first_name = user.get("first_name")
                is_speaker = user.get("is_speaker")
                is_mod = user.get("is_moderator")
                welcome_message = self.set_welcome_message(first_name, user_id)

                if is_speaker and not is_mod:
                    logging.info(f"Attempted to make {first_name} a moderator")
                    self.mod.make_moderator(channel, user_id)

                # The following should probably go elsewhere
                if user_id not in self.already_welcomed_set:
                    run = self.send_room_chat(channel, welcome_message, message_delay)
                    if run:
                        self.already_welcomed_set.add(user_id)

        filtered_users = self.filter_screened_users(user_info, for_mod=True)

        for user in filtered_users:
            user_id = user.get("user_id")

            if user_id in self.mod_list:
                first_name = user.get("first_name")
                is_speaker = user.get("is_speaker")
                is_mod = user.get("is_moderator")
                welcome_message = self.set_welcome_message(first_name, user_id)

                if is_speaker and not is_mod:
                    self.mod.make_moderator(channel, user_id)

                if user_id not in self.already_welcomed_set and user_id not in self.screened_user_set:
                    self.send_room_chat(channel, welcome_message, message_delay)
                    self.already_welcomed_set.add(user_id)

            self.screened_for_mod_set.add(user_id)

    def set_announcement(self, channel, message, interval, delay=None):

        @self.set_interval(interval * 60)
        def announcement():
            response = self.send_room_chat(channel, message, delay)
            return response

        return announcement()

    def set_url_announcement(self, channel, interval=60, delay=2):

        message_1 = "The share url for this room is:"
        message_2 = f"https://www.clubhouse.com/room/{channel}"
        message = [message_1, message_2]

        self.send_room_chat(channel, message, delay)

        @self.set_interval(interval * 60)
        def announcement():
            response = self.send_room_chat(channel, message, delay)
            return response

        return announcement()

    @staticmethod
    def get_time_created(join_info):
        creator_id = join_info.get("creator_user_profile_id")
        host_info = [_ for _ in join_info.get("users") if _.get("user_id") == creator_id]

        if host_info:
            host_info = host_info[0]
        else:
            host_info = join_info.get("users")[0]

        earliest_speaker = [_ for _ in join_info.get("users") if not _.get("is_moderator")]
        if earliest_speaker:
            earliest_speaker = earliest_speaker[0]
        else:
            earliest_speaker = host_info

        host_time = datetime.strptime(host_info.get("time_joined_as_speaker"), "%Y-%m-%dT%H:%M:%S.%f%z")
        earliest_speaker_time = datetime.strptime(earliest_speaker.get("time_joined_as_speaker"), "%Y-%m-%dT%H:%M:%S.%f%z")

        earliest_recorded_time = min(host_time, earliest_speaker_time)
        # tz_aware = pytz.timezone('US/Eastern').localize(eastern_time)

        logging.info(earliest_recorded_time)
        return earliest_recorded_time

    def set_runtime_announcement(self, channel, interval=30, delay=2):
        current_time = pytz.timezone('US/Central').localize(datetime.now())
        running_time = current_time - self.time_created
        time_string = str(running_time).split(".")[0]

        message = f"This room has been running for {time_string}."
        logging.info(message)

        self.send_room_chat(channel, message, delay)

        @self.set_interval(interval * 60)
        def announcement():
            response = self.send_room_chat(channel, message, delay)
            return response

        return announcement()

    @staticmethod
    def get_channel_type(join_info):
        if join_info.get("is_private"):
            channel_type = "private"
        elif join_info.get("is_social_mode"):
            channel_type = "social"
        else:
            channel_type = "public"

        logging.info(channel_type)
        return channel_type

    @staticmethod
    def get_club(join_info):
        club = join_info.get("club")
        if not club:
            return

        club_id = club.get("club_id")

        logging.info(club_id)
        return club_id

    @staticmethod
    def get_auto_speaker_approval(join_info):
        auto_speaker_approval = join_info.get("is_automatic_speaker_approval_available")
        logging.info(auto_speaker_approval)
        return auto_speaker_approval

    @staticmethod
    def get_token(join_info):
        token = join_info.get("token")  # For audio client
        logging.info(token)
        return token

    def terminate_channel(self, channel):
        self.channel.leave(channel)

        if self.keep_alive_thread:
            self.keep_alive_thread.set()

        if self.announcement_thread:
            self.announcement_thread.set()

        self.waiting_speaker = False
        self.granted_speaker = False
        self.active_speaker = False
        self.waiting_mod = False
        self.granted_mod = False
        self.active_mod = False

        self.url = None
        self.channel_type = None
        self.host_name = None
        self.club_id = None
        self.auto_speaker_approval = None
        self.time_created = None
        self.token = None
        self.chat_enabled = None

        self.url_announcement = False
        self.in_automod_club = False
        self.in_social_club = False

        self.screened_user_set = set()
        self.unscreened_user_set = set()
        self.screened_for_speaker_set = set()
        self.screened_for_mod_set = set()
        self.already_welcomed_set = set()
        self.filtered_users_list = []

    automod_clubs = set(Config.config_to_list(Config.load_config(), "AutoModClubs", True))
    social_clubs = set(Config.config_to_list(Config.load_config(), "SocialClubs", True))
    # self.respond_ping_list = set(Config.config_to_list(Config.load_config(), "RespondPing", True))
    mod_list = set(Config.config_to_list(Config.load_config(), "ModList", True))
    guest_list = set(
        (Config.config_to_list(Config.load_config(), "GuestList", True)
         + Config.config_to_list(Config.load_config(), "ASocialRoomGuestList", True)))

    url = None
    host_name = None
    creator_id = None
    channel_type = None
    club_id = None
    chat_enabled = None
    auto_speaker_approval = None
    time_created = None
    token = None

    active_channel = False
    waiting_speaker = False
    granted_speaker = False
    active_speaker = False
    waiting_mod = False
    granted_mod = False
    active_mod = False

    screened_user_set = set()
    unscreened_user_set = set()
    screened_for_speaker_set = set()
    screened_for_mod_set = set()
    already_welcomed_set = set()
    filtered_users_list = []

    url_announcement = False
    in_automod_club = False
    in_social_club = False

    waiting_speaker_thread = None
    waiting_mod_thread = None
    waiting_reconnect_thread = None

    announcement_thread = None
    url_announcement_thread = None
    runtime_announcement_thread = None
    # music_thread = None
    welcome_thread = None
    keep_alive_thread = None
    chat_client_thread = None

    # attempted_ping_response = set()

    # s3_dump_interval = 0
    s3_dump_counter = 0
