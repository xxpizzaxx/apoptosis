import json
import urllib

from datetime import datetime

import tornado.web
import tornado.httpclient

from hkauth.log import app_log, sec_log

from hkauth.services import slack
from hkauth.services import ts3

from hkauth.http.base import (
    AuthPage
)

from hkauth.eve.crest import default_scopes

from hkauth.models import session
from hkauth.models import (
    UserModel,
    UserLoginModel,
    CharacterModel,
    TS3IdentityModel,
    SlackIdentityModel,
    GroupModel,
    MembershipModel
)

from hkauth.cache import redis_cache

from hkauth import config


# XXX fix newline by using base64 module
sso_auth = "{}:{}".format(config.evesso_clientid, config.evesso_secretkey).encode("base64").replace("\n", "")
sso_login = "https://login.eveonline.com/oauth/authorize?" + urllib.urlencode({
    "response_type": "code",
    "redirect_uri": config.evesso_callback,
    "client_id": config.evesso_clientid,
    "scope": " ".join(default_scopes),
    "state": "foo"  # XXX make JWT
})


class LoginPage(AuthPage):
    def get(self):
        if self.current_user:  # XXX
            raise tornado.web.HTTPError(403)

        return self.render("login.html", login_url=sso_login)


class LoginCallbackPage(AuthPage):
    def get(self):
        # XXX do this depending on the code

        # This callback can go two ways depending on the fact if someone
        # is already logged in. If they are we are adding a character to
        # their account. If they are not we are either logging them in
        # or creating their account.

        code = self.get_argument("code", None)
        state = self.get_argument("state", None)

        if not code or not state:
            return  # XXX

        # XXX clean code!?
        # XXX check state!?

        if self.current_user:
            return self._add()
        else:
            return self._login()

    def _sso_response(self):
        code = self.get_argument("code", None)
        state = self.get_argument("state", None)

        if not code or not state:
            return  # XXX

        sso_client = tornado.httpclient.HTTPClient()
        request = tornado.httpclient.HTTPRequest(
            "https://login.eveonline.com/oauth/token",
            method="POST",
            headers={
                "Authorization": "Basic {}".format(sso_auth),
                "Content-Type": "application/json"
            },
            body=json.dumps({
                "grant_type": "authorization_code",
                "code": code
            })
        )

        response = json.loads(sso_client.fetch(request).body)

        access_token = response["access_token"]
        refresh_token = response["refresh_token"]

        # Abstract (can be skipped?) XXX
        request = tornado.httpclient.HTTPRequest(
            "https://login.eveonline.com/oauth/verify",
            headers={
                "Authorization": "Bearer {}".format(access_token),
                "User-Agent": "Hard Knocks Inc. Authentication System"
            }
        )

        response = json.loads(sso_client.fetch(request).body)

        character_id = response["CharacterID"]
        character_scopes = response["Scopes"].split(" ")

        return character_id, character_scopes, access_token, refresh_token

    def _create(self, character_id, character_scopes, access_token, refresh_token): 
        # We don't have an account with this character on it yet. Let's fetch the 
        # character information from the XML API and fill it into a model, tie it
        # up to a fresh new user and log it in
        character = CharacterModel.from_xml_api(character_id)
        character.access_token = access_token
        character.refresh_token = refresh_token

        # For our scopes we see if they already exist, if they don't we create them and hang them
        # on the character
        character.update_scopes(character_scopes)

        return character

    def _add(self):
        character_id, character_scopes, access_token, refresh_token = self._sso_response()

        # See if we already have this character, XXX main and accounthash?
        character = session.query(CharacterModel).filter(CharacterModel.character_id==character_id).first()

        if character: # XXX add new scopes
            if character.user == self.current_user:
                # update scopes
                a = 1
            else:
                sec_log.warn("user {} tried to add {} but belongs to {}".format(self.current_user, character, character.user))
                raise tornado.web.HTTPError(403)
        else:
            character = self._create(character_id, character_scopes, access_token, refresh_token)

        # Append the character to the currently logged in character
        self.current_user.characters.append(character)
        self.current_user.chg_date = datetime.now()

        session.add(self.current_user)
        session.commit()

        sec_log.info("added %s for %s" % (character, character.user))

        self.redirect("/characters/add/success")

    def _login(self):
        character_id, character_scopes, access_token, refresh_token = self._sso_response()

        # See if we already have this character, XXX main and accounthash?
        character = session.query(CharacterModel).filter(CharacterModel.character_id==character_id).first()

        # The character already exists so we log in to the corresponding user and
        # redirect to the success page # XXX verify accountHash?
        if character: # XXX add new scopes
            sec_log.info("logged in %s through %s" % (character.user, character))
            self.set_current_user(character.user)

            login = UserLoginModel()
            login.user = character.user
            login.pub_date = datetime.now()
            login.ip_address = self.request.remote_ip

            session.add(login)
            session.commit()

            return self.redirect("/login/success")

        # We don't have an account with this character on it yet. Let's fetch the 
        # character information from the XML API and fill it into a model, tie it
        # up to a fresh new user and log it in
        character = self._create(character_id, character_scopes, access_token, refresh_token)
        character.is_main = True
        character.pub_date = datetime.now()

        user = UserModel()
        user.characters.append(character)
        user.pub_date = datetime.now()
        user.chg_date = datetime.now()

        session.add(user)
        session.commit()

        self.set_current_user(user)

        sec_log.info("created %s through %s" % (character.user, character))

        login = UserLoginModel()
        login.user = character.user
        login.pub_date = datetime.now()
        login.ip_address = self.request.remote_ip

        session.add(login)
        session.commit()

        # Redirect to another page with some more information for the user of what
        # is going on
        self.redirect("/login/created")


class LoginSuccessPage(AuthPage):
    def get(self):
        self.requires_login()

        return self.render("login_success.html")


class LoginCreatedPage(AuthPage):
    def get(self):
        self.requires_login()

        return self.render("login_created.html")

class LogoutPage(AuthPage):
    def post(self):
        self.set_current_user(None)

        return self.redirect("/logout/success")

class LogoutSuccessPage(AuthPage):
    def get(self):
        return self.render("logout_success.html")


class HomePage(AuthPage):
    def get(self):
        return self.render("home.html")


class CharactersPage(AuthPage):
    def get(self):
        self.requires_login()

        login_url = sso_login

        return self.render("characters.html", login_url=login_url)


class CharactersSelectMainPage(AuthPage):
    def post(self):
        self.requires_login()

        character = self.model_by_id(CharacterModel, "character_id")

        for char in self.current_user.characters:
            char.is_main = False

        character.is_main = True
        
        session.add(self.current_user)
        session.commit()

        # TRIGGER LDAP

        return self.redirect("/characters/select_main/success")


class CharactersSelectMainSuccessPage(AuthPage):
    def get(self):
        self.requires_login()

        return self.render("characters_select_main_success.html")


class ServicesPage(AuthPage):
    def get(self):
        self.requires_login()

        return self.render("services.html")


class ServicesAddTS3IdentityPage(AuthPage):
    def post(self):
        self.requires_login()

        teamspeak_id = self.get_argument("teamspeak_id", None)

        if not teamspeak_id: # XXX
            raise tornado.web.HTTPError(400)

        ts3identity = TS3IdentityModel(teamspeak_id)
        ts3identity.user = self.current_user

        session.add(ts3identity)
        session.commit()

        sec_log.info("ts3identity {} added to {}".format(ts3identity, ts3identity.user))

        return self.redirect("/services/add_teamspeak_identity/success?ts3identity_id={id}".format(id=ts3identity.id))


class ServicesAddTS3IdentitySuccessPage(AuthPage):
    def get(self):
        self.requires_login()

        ts3identity = self.model_by_id(TS3IdentityModel, "ts3identity_id")

        return self.render("services_add_teamspeak_identity_success.html", ts3identity=ts3identity)


class ServicesAddSlackIdentityPage(AuthPage):
    def post(self):
        self.requires_login()

        slack_id = self.get_argument("slack_id", None)

        if not slack_id: # XXX
            raise tornado.web.HTTPError(400)

        slackidentity = SlackIdentityModel(slack_id)
        slackidentity.user = self.current_user

        session.add(slackidentity)
        session.commit()

        sec_log.info("slackidentity {} added to {}".format(slackidentity, slackidentity.user))

        return self.redirect("/services/add_slack_identity/success?slackidentity_id={id}".format(id=slackidentity.id))


class ServicesAddSlackIdentitySuccessPage(AuthPage):
    def get(self):
        self.requires_login()

        slackidentity = self.model_by_id(SlackIdentityModel, "slackidentity_id")

        return self.render("services_add_slack_identity_success.html", slackidentity=slackidentity)


class ServicesSendVerificationSlackIdentityPage(AuthPage):
    def post(self):
        self.requires_login()

        slackidentity = self.model_by_id(SlackIdentityModel, "slackidentity_id")

        # Send verification
        slack.send_verification(slackidentity.email)

        sec_log.info("slackidentity {} for {} sent verification".format(slackidentity, slackidentity.user))

        return self.render("services_verify_slack_identity.html", slackidentity=slackidentity)


class ServicesVerifyVerificationSlackIdentityPage(AuthPage):
    def post(self):
        self.requires_login()

        code = self.get_argument("code", None)

        slackidentity = self.model_by_id(SlackIdentityModel, "slackidentity_id")

        if slackidentity.verification_code == code:
            slackidentity.verification_done = True

            session.add(slackidentity)
            session.commit()

            sec_log.info("slackidentity {} for {} verified".format(slackidentity, slackidentity.user))

            return self.redirect("/services/verify_slack_verification/success?slackidentity_id={}".format(slackidentity.id))
        else:
            return self.redirect("/services/verify_slack_verification/failure?slackidentity_id={}".format(slackidentity.id))


class ServicesVerifySlackIdentitySuccessPage(AuthPage):
    def get(self):
        self.requires_login()

        slackidentity = self.model_by_id(SlackIdentityModel, "slackidentity_id")

        return self.render("services_verify_slack_identity_success.html", slackidentity=slackidentity)


class GroupsPage(AuthPage):
    def get(self):
        self.requires_login()
        self.requires_internal()

        groups = session.query(GroupModel).all()

        return self.render("groups.html", groups=groups)


class GroupsJoinPage(AuthPage):
    def post(self):
        self.requires_login()
        self.requires_internal()

        group = self.model_by_id(GroupModel, "group_id")

        membership = MembershipModel()
        membership.user = self.current_user
        membership.group = group

        if group.requires_approval:
            membership.pending = True

        session.add(membership)
        session.commit()

        sec_log.info("user {} joined group {}".format(membership.user, membership.group))

        # XXX Update the entire group
        slack.group_upkeep(group.slug, [member.slack_identities[0].email for member in group.members if len(member.slack_identities)])

        return self.redirect("/groups/join/success?membership_id={}".format(membership.id))


class GroupsJoinSuccessPage(AuthPage):
    def get(self):
        self.requires_login()
        self.requires_internal()

        membership = self.model_by_id(MembershipModel, "membership_id")

        return self.render("groups_join_success.html", membership=membership)


class GroupsLeavePage(AuthPage):
    def post(self):
        self.requires_login()
        self.requires_internal()

        group = self.model_by_id(GroupModel, "group_id")

        for membership in group.memberships:
            if membership.user == self.current_user:
                session.delete(membership)
                session.commit()

                break
        else:
            raise tornado.web.HTTPError(400)

        sec_log.info("user {} left group {}".format(membership.user, membership.group))

        # XXX Update the entire group
        slack.group_upkeep(group.slug, [member.slack_identities[0].email for member in group.members if len(member.slack_identities)])

        return self.redirect("/groups/leave/success?group_id={}".format(group.id))


class GroupsLeaveSuccessPage(AuthPage):
    def get(self):
        self.requires_login()
        self.requires_internal()

        group = self.model_by_id(GroupModel, "group_id")

        return self.render("groups_leave_success.html", group=group)


class PingPage(AuthPage):
    def get(self):
        self.requires_login()
        self.requires_internal()

        user_groups = self.current_user.groups

        return self.render("ping.html", groups=user_groups)

class PingSendAllPage(AuthPage):
    def post(self):
        self.requires_login()
        self.requires_internal()

        message = self.get_argument("message", None)

        if not message:
            raise tornado.web.HTTPError(500)

        slack.group_ping("midnight-rodeo", message)

        app_log.info(
            "%s sent all ping: %s" % (self.current_user, message)
        )

        return self.redirect("/ping/send_all/success")


class PingSendAllSuccessPage(AuthPage):
    def get(self):
        self.requires_login()
        self.requires_internal()

        return self.render("ping_all_success.html")


class PingSendGroupPage(AuthPage):
    def post(self):
        self.requires_login()
        self.requires_internal()

        message = self.get_argument("message", None)
        group = self.model_by_id(GroupModel, "group_id")

        if not message:
            raise tornado.web.HTTPError(500)

        slack.group_ping(group.slug, message)

        app_log.info(
            "%s sent group ping to %s: %s" % (self.current_user, group, message)
        )

        return self.redirect("/ping/send_group/success?group_id={}".format(group.id))


class PingSendGroupSuccessPage(AuthPage):
    def get(self):
        self.requires_login()
        self.requires_internal()

        group = self.model_by_id(GroupModel, "group_id")

        return self.render("ping_group_success.html", group=group)


class AdminUsersPage(AuthPage):
    def get(self):
        self.requires_login()
        self.requires_internal()

        users = session.query(UserModel).all()

        return self.render("admin_users.html", users=users)


class AdminGroupsPage(AuthPage):
    def get(self):
        self.requires_login()
        self.requires_internal()

        groups = session.query(GroupModel).all()

        return self.render("admin_groups.html", groups=groups)
