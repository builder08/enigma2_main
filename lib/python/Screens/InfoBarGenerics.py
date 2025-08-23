# -*- coding: utf-8 -*-
from Screens.ChannelSelection import ChannelSelection, BouquetSelector, SilentBouquetSelector

from Components.ActionMap import ActionMap, HelpableActionMap, HelpableNumberActionMap, NumberActionMap
from Components.AVSwitch import avSwitch
from Components.Harddisk import harddiskmanager, findMountPoint
from Components.Input import Input
from Components.Label import Label
from Components.MovieList import AUDIO_EXTENSIONS, MOVIE_EXTENSIONS, DVD_EXTENSIONS
from Components.Pixmap import MovingPixmap
from Components.PluginComponent import plugins
from Components.ServiceEventTracker import ServiceEventTracker
from Components.Sources.ServiceEvent import ServiceEvent
from Components.Sources.Boolean import Boolean
from Components.config import config, ConfigBoolean, ConfigClock, ACTIONKEY_RIGHT
from Components.SystemInfo import BoxInfo, getBoxDisplayName
from Components.UsageConfig import preferredInstantRecordPath, defaultMoviePath
from Components.VolumeControl import VolumeControl
from Components.Renderer.PositionGauge import PositionGauge
from Components.Renderer.Progress import Progress
from Components.Sources.StaticText import StaticText
from Components.Task import job_manager
from Screens.EpgSelection import EPGSelection
from Plugins.Plugin import PluginDescriptor

from Screens.Screen import Screen
from Screens.ScreenSaver import InfoBarScreenSaver
from Screens import Standby
from Screens.ChoiceBox import ChoiceBox
from Screens.Dish import Dish
from Screens.EventView import EventViewEPGSelect, EventViewSimple
from Screens.InputBox import InputBox
from Screens.MessageBox import MessageBox
from Screens.MinuteInput import MinuteInput
from Screens.TimerSelection import TimerSelection
from Screens.PictureInPicture import PictureInPicture
from Screens.PiPSetup import PiPSetup
from Screens.PVRState import PVRState, TimeshiftState
from Screens.SubtitleDisplay import SubtitleDisplay
from Screens.RdsDisplay import RdsInfoDisplay, RassInteractive
from Screens.TimeDateInput import TimeDateInput
from Screens.UnhandledKey import UnhandledKey
from ServiceReference import ServiceReference, getStreamRelayRef, hdmiInServiceRef, isPlayableForCur

from Tools.ASCIItranslit import legacyEncode
from Tools.Directories import SCOPE_CONFIG, SCOPE_SKINS, fileExists, fileReadLines, fileWriteLines, fileReadLinesISO, getRecordingFilename, moveFiles, resolveFilename
from keyids import KEYFLAGS, KEYIDS, KEYIDNAMES
from Tools.Notifications import AddPopup, AddNotificationWithCallback, current_notifications, lock, notificationAdded, notifications, RemovePopup
from Tools.BoundFunction import boundFunction

from keyids import KEYFLAGS, KEYIDNAMES, KEYIDS

from enigma import eActionMap, eAVControl, eDBoxLCD, eDVBDB, eDVBServicePMTHandler, eDVBVolumecontrol, eEPGCache, eServiceCenter, eServiceReference, eTimer, getBsodCounter, getDesktop, iPlayableService, iServiceInformation, quitMainloop, resetBsodCounter
from skin import findSkinScreen
from time import time, localtime, strftime
import Screens.Standby
from inspect import getfullargspec
import os
from os.path import exists, isfile, ismount, realpath, splitext
from bisect import insort
from sys import maxsize
import itertools
import datetime
from re import match
from pickle import load as pickle_load, dump as pickle_dump

from RecordTimer import RecordTimer, RecordTimerEntry, findSafeRecordPath, parseEvent

# hack alert!
from Screens.Menu import MainMenu, mdom

MODEL = BoxInfo.getItem("model")

MODULE_NAME = __name__.split(".")[-1]

AUDIO = False
# seek_withjumps_muted = False
jump_pts_adder = 0
jump_last_pts = None
jump_last_pos = None


def isStandardInfoBar(self):
	return self.__class__.__name__ == "InfoBar"


class ResumePoints():
	def __init__(self):
		self.resumePointFile = "/etc/enigma2/resumepoints.pkl"
		self.resumePointCache = {}
		self.loadResumePoints()
		self.cacheCleanTimer = eTimer()
		self.cacheCleanTimer.callback.append(self.cleanCache)
		self.cleanCache()  # get rid of stale entries on reboot

	def loadResumePoints(self):
		self.resumePointCache.clear()
		if fileExists(self.resumePointFile):
			with open(self.resumePointFile, "rb") as f:
				self.resumePointCache.update(pickle_load(f, fix_imports=True, encoding="utf8"))

	def saveResumePoints(self):
		with open(self.resumePointFile, "wb") as f:
			pickle_dump(self.resumePointCache, f, protocol=5)

	def delResumePoint(self, ref):
		if (sref := ref.toString()) in self.resumePointCache:
			del self.resumePointCache[sref]
			self.saveResumePoints()

	def cleanCache(self):
		changed = False
		now = int(time())
		self.cacheCleanTimer.stop()
		for sref, v in list(self.resumePointCache.items()):
			if "%3a//" in sref:  # resume point is stream
				if now > v[0] + 7 * 24 * 60 * 60:  # keep stream resume points maximum one week
					del self.resumePointCache[sref]
					changed = True
			else:
				filepath = realpath(sref.split(':')[-1])
				mountpoint = findMountPoint(filepath)
				if ismount(mountpoint) and not exists(filepath):
					del self.resumePointCache[sref]
					changed = True
		if changed:
			self.saveResumePoints()
		self.cacheCleanTimer.startLongTimer(24 * 60 * 60)  # clean up daily

	def setResumePoint(self, session):
		service = session.nav.getCurrentService()
		ref = session.nav.getCurrentlyPlayingServiceOrGroup()
		if service is not None and ref is not None:  # and (ref.type != 1):
			# ref type 1 has its own memory...
			seek = service.seek()
			if seek:
				pos = seek.getPlayPosition()
				if not pos[0]:
					sref = ref.toString()
					sl = x[1] if (x := seek.getLength()) else None
					self.resumePointCache[sref] = [int(time()), pos[1], sl]
					self.saveResumePoints()

	def getResumePoint(self, session):
		ref = session.nav.getCurrentlyPlayingServiceOrGroup()
		if (ref is not None) and (ref.type != 1) and (sref := ref.toString()) in self.resumePointCache:
			entry = self.resumePointCache[sref]
			entry[0] = int(time())  # update LRU timestamp
			return entry[1]


resumePointsInstance = ResumePoints()


class whitelist:
	FILENAME_VBI = "/etc/enigma2/whitelist_vbi"
	vbi = []
	FILENAME_BOUQUETS = "/etc/enigma2/whitelist_bouquets"
	bouquets = []


def reload_whitelist_vbi():
	whitelist.vbi = [line.strip() for line in open(whitelist.FILENAME_VBI, 'r').readlines()] if isfile(whitelist.FILENAME_VBI) else []


def reload_whitelist_bouquets():
	whitelist.bouquets = [line.strip() for line in open(whitelist.FILENAME_BOUQUETS, 'r').readlines()] if isfile(whitelist.FILENAME_BOUQUETS) else []


reload_whitelist_vbi()
reload_whitelist_bouquets()

class InfoBarStreamRelay:

	FILENAME = "/etc/enigma2/whitelist_streamrelay"

	def __init__(self):
		self.__srefs = self.__sanitizeData(open(self.FILENAME, 'r').readlines()) if os.path.isfile(self.FILENAME) else []

	def __sanitizeData(self, data):
		return list(set([line.strip() for line in data if line and isinstance(line, str) and match("^(?:[0-9A-F]+[:]){10}$", line.strip())])) if isinstance(data, list) else []

	def __saveToFile(self):
		self.__srefs.sort(key=lambda ref: (int((x := ref.split(":"))[6], 16), int(x[5], 16), int(x[4], 16), int(x[3], 16)))
		open(self.FILENAME, 'w').write('\n'.join(self.__srefs))

	def splitref(self, ref):
		ref = ref.split(":")
		return ":".join(ref[:11]), len(ref) > 11 and ref[-1]

	def check(self, nav, service):
		return (service or nav.getCurrentlyPlayingServiceReference()) and service.toCompareString() in self.__srefs

	def write(self):
		fileWriteLines(self.FILENAME, self.__srefs, source=self.__class__.__name__)

	def toggle(self, nav, service):
		if (servicestring := (service and self.splitref(service.toString())[0])):
			if servicestring in self.__srefs:
				self.__srefs.remove(servicestring)
			else:
				self.__srefs.append(servicestring)
			if nav.getCurrentlyPlayingServiceReference() == service:
				nav.restartService()
			self.__saveToFile()

	def getData(self):
		return self.__srefs

	def setData(self, data):
		self.__srefs = self.__sanitizeData(data)
		self.__saveToFile()

	data = property(getData, setData)

	def streamrelayChecker(self, playref):
		playrefstring = playref.toCompareString()
		if "%3a//" not in playrefstring and playrefstring in self.__srefs:
			url = f'http://{".".join("%d" % d for d in config.misc.softcam_streamrelay_url.value)}:{config.misc.softcam_streamrelay_port.value}/'
			if "127.0.0.1" in url:
				playrefmod = ":".join([("%x" % (int(x[1], 16) + 1)).upper() if x[0] == 6 else x[1] for x in enumerate(playrefstring.split(':'))])
			else:
				playrefmod = playrefstring
			playref = eServiceReference("%s%s%s:%s" % (playrefmod, url.replace(":", "%3a"), playrefstring.replace(":", "%3a"), ServiceReference(playref).getServiceName()))
			print(f"[{self.__class__.__name__}] Play service {playref.toCompareString()} via streamrelay")
			playref.setAlternativeUrl(playrefstring, True)
			return playref, True
		return playref, False

	def checkService(self, service):
		return service and service.toCompareString() in self.__srefs


streamrelay = InfoBarStreamRelay()


class subservice:
	groupslist = None


def reload_subservice_groupslist(force=False):
	if subservice.groupslist is None or force:
		try:
			groupedservices = "/etc/enigma2/groupedservices"
			if not isfile(groupedservices):
				groupedservices = "/usr/share/enigma2/groupedservices"
			subservice.groupslist = [list(g) for k, g in itertools.groupby([line.split('#')[0].strip() for line in open(groupedservices).readlines()], lambda x: not x) if not k]
		except:
			subservice.groupslist = []


reload_subservice_groupslist()


def getPossibleSubservicesForCurrentChannel(current_service):
	if current_service and subservice.groupslist:
		ref_in_subservices_group = [x for x in subservice.groupslist if current_service in x]
		if ref_in_subservices_group:
			return ref_in_subservices_group[0]
	return []


def getActiveSubservicesForCurrentChannel(service):
	activeSubservices = []
	if info := service and service.info():
		sRef = info.getInfoString(iServiceInformation.sServiceref)
		url = "http://%s:%s/" % (config.misc.softcam_streamrelay_url.getHTML(), config.misc.softcam_streamrelay_port.value)
		splittedRef = sRef.split(url.replace(":", "%3a"))
		if len(splittedRef) > 1:
			sRef = splittedRef[1].split(":")[0].replace("%3a", ":")
		current_service = ':'.join(sRef.split(':')[:11])
		if current_service:
			possibleSubservices = getPossibleSubservicesForCurrentChannel(current_service)
			epgCache = eEPGCache.getInstance()
			for subservice in possibleSubservices:
				events = epgCache.lookupEvent(['BDTS', (subservice, 0, -1)])
				if events and len(events) == 1:
					event = events[0]
					title = event[2]
					if title and ("Sendepause" not in title and "Sky Sport Kompakt" not in title):
						starttime = datetime.datetime.fromtimestamp(event[0]).strftime('%H:%M')
						endtime = datetime.datetime.fromtimestamp(event[0] + event[1]).strftime('%H:%M')
						try:
							service_name = ServiceReference(subservice).getServiceName()
						except:
							service_name = ""
						current_show_name = "%s [%s-%s] %s" % (title, str(starttime), str(endtime), service_name)
						activeSubservices.append((current_show_name, subservice))
	if not activeSubservices:
		subservices = service and service.subServices()
		if subservices:
			for idx in range(0, subservices.getNumberOfSubservices()):
				subservice = subservices.getSubservice(idx)
				activeSubservices.append((subservice.getName(), subservice.toString()))
	return activeSubservices


def hasActiveSubservicesForCurrentChannel(service):
	activeSubservices = getActiveSubservicesForCurrentChannel(service)
	return bool(activeSubservices and len(activeSubservices) > 1)


class InfoBarDish:
	def __init__(self):
		self.dishDialog = self.session.instantiateDialog(Dish)
		self.dishDialog.setAnimationMode(0)


class InfoBarUnhandledKey:
	def __init__(self):
		self.unhandledKeyDialog = self.session.instantiateDialog(UnhandledKey)
		self.unhandledKeyDialog.setAnimationMode(0)
		self.hideUnhandledKeySymbolTimer = eTimer()
		self.hideUnhandledKeySymbolTimer.callback.append(self.unhandledKeyDialog.hide)
		self.checkUnusedTimer = eTimer()
		self.checkUnusedTimer.callback.append(self.checkUnused)
		self.onLayoutFinish.append(self.unhandledKeyDialog.hide)
		eActionMap.getInstance().bindAction('', -maxsize - 1, self.actionA)  # highest prio
		eActionMap.getInstance().bindAction('', maxsize, self.actionB)  # lowest prio
		self.flags = (1 << 1)
		self.uflags = 0
		self.sibIgnoreKeys = (
			KEYIDS["KEY_VOLUMEDOWN"], KEYIDS["KEY_VOLUMEUP"],
			KEYIDS["KEY_EXIT"], KEYIDS["KEY_OK"],
			KEYIDS["KEY_UP"], KEYIDS["KEY_DOWN"],
			KEYIDS["KEY_CHANNELUP"], KEYIDS["KEY_CHANNELDOWN"],
			KEYIDS["KEY_NEXT"], KEYIDS["KEY_PREVIOUS"]
		)

	# This function is called on every keypress!
	def actionA(self, key, flag):
		print("[InfoBarGenerics] Key: %s (%s) KeyID='%s'." % (key, KEYFLAGS.get(flag, _("Unknown")), KEYIDNAMES.get(key, _("Unknown"))))
		if flag != 2:  # Don't hide on repeat.
			self.unhandledKeyDialog.hide()
			if self.closeSIB(key) and self.secondInfoBarScreen and self.secondInfoBarScreen.shown:
				self.secondInfoBarScreen.hide()
				self.secondInfoBarWasShown = False
		if flag != 4:
			if flag == 0:
				self.flags = self.uflags = 0
			self.flags |= (1 << flag)
			if flag == 1 or flag == 3:  # Break and Long.
				self.checkUnusedTimer.start(0, True)
		return 0

	def closeSIB(self, key):
		return True if key >= 12 and key not in self.sibIgnoreKeys else False  # (114, 115, 174, 352, 103, 108, 402, 403, 407, 412)

	# this function is only called when no other action has handled this key
	def actionB(self, key, flag):
		if flag != 4:
			self.uflags |= (1 << flag)

	def checkUnused(self):
		if self.flags == self.uflags:
			self.unhandledKeyDialog.show()
			self.hideUnhandledKeySymbolTimer.start(2000, True)


class HideVBILine(Screen):
	def __init__(self, session):
		self.skin = """<screen position="0,0" size="%s,%s" flags="wfNoBorder" zPosition="1"/>""" % (getDesktop(0).size().width(), getDesktop(0).size().height() / 180 + 1)
		Screen.__init__(self, session)


class SecondInfoBar(Screen):
	def __init__(self, session, skinName):
		Screen.__init__(self, session)
		self.skinName = skinName


class InfoBarShowHide(InfoBarScreenSaver):
	""" InfoBar show/hide control, accepts toggleShow and hide actions, might start
	fancy animations. """
	STATE_HIDDEN = 0
	STATE_HIDING = 1
	STATE_SHOWING = 2
	STATE_SHOWN = 3
	FLAG_CENTER_DVB_SUBS = 2048

	def __init__(self):
		self["ShowHideActions"] = ActionMap(["InfobarShowHideActions"],
			{
				"toggleShow": self.okButtonCheck,
				"hide": self.keyHide,
				"toggleShowLong": self.toggleShowLong,
				"hideLong": self.hideLong,
			}, 1)  # lower prio to make it possible to override ok and cancel..

		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evStart: self.serviceStarted,
			})

		InfoBarScreenSaver.__init__(self)
		self.__state = self.STATE_SHOWN
		self.__locked = 0
		self.DimmingTimer = eTimer()
		self.DimmingTimer.callback.append(self.doDimming)
		self.unDimmingTimer = eTimer()
		self.unDimmingTimer.callback.append(self.unDimming)
		self.hideTimer = eTimer()
		self.hideTimer.callback.append(self.doTimerHide)
		self.hideTimer.start(5000, True)

		self.onShow.append(self.__onShow)
		self.onHide.append(self.__onHide)

		self.onShowHideNotifiers = []

		self.actualSecondInfoBarScreen = self.InfoBarAdds = None
		if isStandardInfoBar(self):
			self.secondInfoBarScreen = self.session.instantiateDialog(SecondInfoBar, "SecondInfoBar")
			self.secondInfoBarScreen.show()
			self.secondInfoBarScreen.onShow.append(self.__SecondInfobarOnShow)
			self.secondInfoBarScreen.onHide.append(self.__SecondInfobarOnHide)
			self.secondInfoBarScreenSimple = self.session.instantiateDialog(SecondInfoBar, "SecondInfoBarSimple")
			self.secondInfoBarScreenSimple.show()
			self.secondInfoBarScreenSimple.onShow.append(self.__SecondInfobarOnShow)
			self.secondInfoBarScreenSimple.onHide.append(self.__SecondInfobarOnHide)
			self.actualSecondInfoBarScreen = config.usage.show_simple_second_infobar.value and self.secondInfoBarScreenSimple.skinAttributes and self.secondInfoBarScreenSimple or self.secondInfoBarScreen
			if findSkinScreen("InfoBarAdds"):
				self.InfoBarAdds = self.session.instantiateDialog(SecondInfoBar, "InfoBarAdds")
				self.InfoBarAdds.show()

		self.InfobarPluginScreens = [self.session.instantiateDialog(plugin) for plugin in plugins.getPlugins(where=PluginDescriptor.WHERE_INFOBAR_SCREEN)]
		self.SecondInfobarPluginScreens = [self.session.instantiateDialog(plugin) for plugin in plugins.getPlugins(where=PluginDescriptor.WHERE_SECONDINFOBAR_SCREEN)]

		from Screens.InfoBar import InfoBar
		InfoBarInstance = InfoBar.instance
		if InfoBarInstance:
			InfoBarInstance.hideVBILineScreen.hide()
		self.hideVBILineScreen = self.session.instantiateDialog(HideVBILine)
		self.hideVBILineScreen.show()
		self.lastResetAlpha = True
		self.onLayoutFinish.append(self.__layoutFinished)
		self.onExecBegin.append(self.__onExecBegin)

	def __onExecBegin(self):
		self.showHideVBI()

	def __layoutFinished(self):
		if self.actualSecondInfoBarScreen:
			self.secondInfoBarScreen.hide()
			self.secondInfoBarScreenSimple.hide()
		if self.InfoBarAdds:
			self.InfoBarAdds.hide()
		self.secondInfoBarWasShown = False
		self.hideVBILineScreen.hide()

	def __onShow(self):
		self.__state = self.STATE_SHOWN
		for x in self.onShowHideNotifiers:
			x(True)
		for PluginScreen in self.InfobarPluginScreens:
			PluginScreen.show()
		self.startHideTimer()
		if self.InfoBarAdds and config.usage.show_infobar_adds.value:
			self.InfoBarAdds.show()

	def __onHide(self):
		self.__state = self.STATE_HIDDEN
		self.resetAlpha()
		if self.actualSecondInfoBarScreen:
			self.actualSecondInfoBarScreen.hide()
		for x in self.onShowHideNotifiers:
			x(False)
		for PluginScreen in self.InfobarPluginScreens:
			PluginScreen.hide()
		if self.InfoBarAdds:
			self.InfoBarAdds.hide()

	def __SecondInfobarOnShow(self):
		for PluginScreen in self.InfobarPluginScreens:
			PluginScreen.hide()
		for PluginScreen in self.SecondInfobarPluginScreens:
			PluginScreen.show()

	def __SecondInfobarOnHide(self):
		for PluginScreen in self.SecondInfobarPluginScreens:
			PluginScreen.hide()

	def resetAlpha(self):
		if config.usage.show_infobar_do_dimming.value and self.lastResetAlpha is False:
			self.unDimmingTimer.start(300, True)

	def doDimming(self):
		if config.usage.show_infobar_do_dimming.value:
			self.dimmed = int(int(self.dimmed) - 1)
		else:
			self.dimmed = 0
		self.DimmingTimer.stop()
		self.doHide()

	def unDimming(self):
		self.unDimmingTimer.stop()
		self.doWriteAlpha(config.av.osd_alpha.value)

	def doWriteAlpha(self, value):
		if exists("/proc/stb/video/alpha"):
			f = open("/proc/stb/video/alpha", "w")
			f.write("%i" % (value))
			f.close()
			if value == config.av.osd_alpha.value:
				self.lastResetAlpha = True
			else:
				self.lastResetAlpha = False

	def toggleShowLong(self):
		if not config.usage.ok_is_channelselection.value:
			self.toggleViews()

	def hideLong(self):
		if config.usage.ok_is_channelselection.value:
			self.toggleViews()

	def toggleViews(self):
		if self.shown:
			self.toggleInfoBarAddon()
		else:
			self.toggleSecondInfoBar()

	def toggleSecondInfoBar(self):
		if self.actualSecondInfoBarScreen and not self.actualSecondInfoBarScreen.shown and self.secondInfoBarScreenSimple.skinAttributes and self.secondInfoBarScreen.skinAttributes:
			self.actualSecondInfoBarScreen.hide()
			config.usage.show_simple_second_infobar.value = not config.usage.show_simple_second_infobar.value
			config.usage.show_simple_second_infobar.save()
			self.actualSecondInfoBarScreen = config.usage.show_simple_second_infobar.value and self.secondInfoBarScreenSimple or self.secondInfoBarScreen
			self.showSecondInfoBar()

	def toggleInfoBarAddon(self):
		if self.InfoBarAdds and (self.actualSecondInfoBarScreen and not self.actualSecondInfoBarScreen.shown or not self.actualSecondInfoBarScreen):
			config.usage.show_infobar_adds.value = not config.usage.show_infobar_adds.value
			config.usage.show_infobar_adds.save()
			if config.usage.show_infobar_adds.value:
				self.InfoBarAdds.show()
			else:
				self.InfoBarAdds.hide()

	def keyHide(self):
		if self.__state == self.STATE_HIDDEN and self.session.pipshown and "popup" in config.usage.pip_hideOnExit.value:
			if config.usage.pip_hideOnExit.value == "popup":
				self.session.openWithCallback(self.hidePipOnExitCallback, MessageBox, _("Disable Picture in Picture"), simple=True)
			else:
				self.hidePipOnExitCallback(True)
		elif config.usage.ok_is_channelselection.value and hasattr(self, "openServiceList"):
			self.toggleShow()
		elif self.__state == self.STATE_SHOWN:
			self.hide()

	def hidePipOnExitCallback(self, answer):
		if answer:
			self.showPiP()

	def connectShowHideNotifier(self, fnc):
		if not fnc in self.onShowHideNotifiers:
			self.onShowHideNotifiers.append(fnc)

	def disconnectShowHideNotifier(self, fnc):
		if fnc in self.onShowHideNotifiers:
			self.onShowHideNotifiers.remove(fnc)

	def serviceStarted(self):
		if self.execing:
			if config.usage.show_infobar_on_zap.value:
				self.doShow()
		self.showHideVBI()

	def startHideTimer(self):
		if self.__state == self.STATE_SHOWN and not self.__locked:
			self.hideTimer.stop()
			if self.actualSecondInfoBarScreen and self.actualSecondInfoBarScreen.shown:
				idx = config.usage.show_second_infobar.index - 1
			else:
				idx = config.usage.infobar_timeout.index
			if idx:
				self.hideTimer.startLongTimer(idx)

	def doShow(self):
		self.show()
		self.hideTimer.stop()
		self.DimmingTimer.stop()
		self.doWriteAlpha(config.av.osd_alpha.value)
		self.startHideTimer()

	def doTimerHide(self):
		self.hideTimer.stop()
		self.DimmingTimer.start(300, True)
		self.dimmed = config.usage.show_infobar_dimming_speed.value

	def doHide(self):
		if self.__state != self.STATE_HIDDEN:
			if self.dimmed > 0:
				self.doWriteAlpha((config.av.osd_alpha.value * self.dimmed / config.usage.show_infobar_dimming_speed.value))
				self.DimmingTimer.start(5, True)
			else:
				self.DimmingTimer.stop()
				self.hide()

	def okButtonCheck(self):
		if config.usage.ok_is_channelselection.value and hasattr(self, "openServiceList"):
			if isinstance(self, InfoBarTimeshift) and self.timeshiftEnabled() and isinstance(self, InfoBarSeek) and self.seekstate == self.SEEK_STATE_PAUSE:
				return
			self.openServiceList()
		else:
			self.toggleShow()

	def toggleShow(self):
		if self.__state == self.STATE_HIDDEN:
			self.showFirstInfoBar()
		else:
			self.showSecondInfoBar()

	def showSecondInfoBar(self):
		if isStandardInfoBar(self) and config.usage.show_second_infobar.value == "EPG":
			if not (hasattr(self, "hotkeyGlobal") and self.hotkeyGlobal("info") != 0):
				self.showDefaultEPG()
		elif self.actualSecondInfoBarScreen and config.usage.show_second_infobar.value != "no" and not self.actualSecondInfoBarScreen.shown:
			self.show()
			self.actualSecondInfoBarScreen.show()
			self.startHideTimer()
		else:
			self.hide()
			self.hideTimer.stop()

	def showFirstInfoBar(self):
		if self.__state == self.STATE_HIDDEN or self.actualSecondInfoBarScreen and self.actualSecondInfoBarScreen.shown:
			self.actualSecondInfoBarScreen and self.actualSecondInfoBarScreen.hide()
			self.show()
		else:
			self.hide()
			self.hideTimer.stop()

	def lockShow(self):
		self.__locked += 1
		if self.execing:
			self.show()
			self.hideTimer.stop()

	def unlockShow(self):
		if config.usage.show_infobar_do_dimming.value and self.lastResetAlpha is False:
			self.doWriteAlpha(config.av.osd_alpha.value)
		try:
			self.__locked -= 1
		except Exception:
			self.__locked = 0
		if self.__locked < 0:
			self.__locked = 0
		if self.execing:
			self.startHideTimer()


	def checkHideVBI(self, service=None):
		service = service or self.session.nav.getCurrentlyPlayingServiceReference()
		servicepath = service and service.getPath()
		if servicepath:
			if servicepath.startswith("/"):
				if service.toString().startswith("1:"):
					info = eServiceCenter.getInstance().info(service)
					service = info and info.getInfoString(service, iServiceInformation.sServiceref)
					service = service and eServiceReference(service)
					if service:
						print(service, service and service.toString())
					return service and ":".join(service.toString().split(":")[:11]) in whitelist.vbi
				else:
					return ".hidevbi." in servicepath.lower()
		return service and service.toString() in whitelist.vbi

	def checkBouquets(self, bouquet):
		try:
			return bouquet.toString().split('"')[1] in whitelist.bouquets
		except:
			return

	def checkStreamrelay(self, service):
		return streamrelay.checkService(service)

	def showHideVBI(self):
		if self.checkHideVBI():
			self.hideVBILineScreen.show()
		else:
			self.hideVBILineScreen.hide()

	def ToggleHideVBI(self, service=None):
		service = service or self.session.nav.getCurrentlyPlayingServiceReference()
		if service:
			service = service.toString()
			if service in whitelist.vbi:
				whitelist.vbi.remove(service)
			else:
				whitelist.vbi.append(service)
			open(whitelist.FILENAME_VBI, 'w').write('\n'.join(whitelist.vbi))
			self.showHideVBI()

	def ToggleBouquet(self, bouquet):
		if bouquet in whitelist.bouquets:
			whitelist.bouquets.remove(bouquet)
		else:
			whitelist.bouquets.append(bouquet)
		open(whitelist.FILENAME_BOUQUETS, 'w').write('\n'.join(whitelist.bouquets))

	def checkStreamrelay(self, service=None):
		return streamrelay.check(self.session.nav, service)

	def ToggleStreamrelay(self, service=None):
		streamrelay.toggle(self.session.nav, service)


class BufferIndicator(Screen):
	def __init__(self, session):
		Screen.__init__(self, session)
		self["status"] = Label()
		self.mayShow = False
		self.mayShowTimer = eTimer()
		self.mayShowTimer.callback.append(self.mayShowEndTimer)
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evBuffering: self.bufferChanged,
				iPlayableService.evStart: self.__evStart,
				iPlayableService.evGstreamerPlayStarted: self.__evGstreamerPlayStarted,
			})

	def bufferChanged(self):
		if self.mayShow:
			value = self.getBufferValue()
			if value and value != 100:
				self["status"].setText(_("Buffering %d%%") % value)
				if not self.shown:
					self.show()

	def __evStart(self):
		self.hide()
		self.mayShow = False
		self.mayShowTimer.start(1000, True)

	def __evGstreamerPlayStarted(self):
		self.mayShow = False
		self.mayShowTimer.stop()
		self.hide()

	def mayShowEndTimer(self):
		self.mayShow = True
		if self.getBufferValue() == 0:
			self["status"].setText(_("No data received yet"))
			self.show()

	def getBufferValue(self):
		service = self.session.nav.getCurrentService()
		info = service and service.info()
		return info and info.getInfo(iServiceInformation.sBuffer)


class InfoBarBuffer:
	def __init__(self):
		self.bufferScreen = self.session.instantiateDialog(BufferIndicator)
		self.bufferScreen.hide()


class NumberZap(Screen):
	def quit(self):
		self.Timer.stop()
		self.close()

	def keyOK(self):
		self.Timer.stop()
		self.close(self.service, self.bouquet)

	def handleServiceName(self):
		if self.searchNumber:
			self.service, self.bouquet = self.searchNumber(int(self["number"].getText()))
			self["servicename"].text = self["servicename_summary"].text = ServiceReference(self.service).getServiceName() if ServiceReference(self.service).getServiceName() else _("There is no service with this number")
			self["Service"].newService(self.service)
			if not self.startBouquet:
				self.startBouquet = self.bouquet

	def keyBlue(self):
		if int(config.misc.zapkey_delay.value) > 0:
			self.Timer.start(int(1000 * int(config.misc.zapkey_delay.value)), True)
		if self.searchNumber:
			if self.startBouquet == self.bouquet:
				self.service, self.bouquet = self.searchNumber(int(self["number"].getText()), firstBouquetOnly=True)
			else:
				self.service, self.bouquet = self.searchNumber(int(self["number"].getText()))
			self["servicename"].text = self["servicename_summary"].text = ServiceReference(self.service).getServiceName() if ServiceReference(self.service).getServiceName() else _("There is no service with this number")
			self["Service"].newService(self.service)

	def keyNumberGlobal(self, number):
		if int(config.misc.zapkey_delay.value) > 0:
			self.Timer.start(int(1000 * int(config.misc.zapkey_delay.value)), True)
		self.numberString += str(number)
		self["number"].text = self["number_summary"].text = self.numberString

		self.handleServiceName()

		if len(self.numberString) >= config.usage.numberZapDigits.value:
			self.keyOK()

	def __init__(self, session, number, searchNumberFunction=None):
		Screen.__init__(self, session)
		self.numberString = str(number)
		self.searchNumber = searchNumberFunction
		self.startBouquet = None

		self["channel"] = Label(_("Channel") + ":")
		self["number"] = Label(self.numberString)
		self["servicename"] = Label()
		self["channel_summary"] = StaticText(_("Channel") + ":")
		self["number_summary"] = StaticText(self.numberString)
		self["servicename_summary"] = StaticText()
		self["Service"] = ServiceEvent()

		self.onLayoutFinish.append(self.handleServiceName)
		if config.misc.numzap_picon.value:
			self.skinName = ["NumberZapPicon", "NumberZap"]

		self["actions"] = NumberActionMap(["SetupActions", "ShortcutActions"],
			{
				"cancel": self.quit,
				"ok": self.keyOK,
				"blue": self.keyBlue,
				"1": self.keyNumberGlobal,
				"2": self.keyNumberGlobal,
				"3": self.keyNumberGlobal,
				"4": self.keyNumberGlobal,
				"5": self.keyNumberGlobal,
				"6": self.keyNumberGlobal,
				"7": self.keyNumberGlobal,
				"8": self.keyNumberGlobal,
				"9": self.keyNumberGlobal,
				"0": self.keyNumberGlobal
			})

		self.Timer = eTimer()
		self.Timer.callback.append(self.keyOK)
		if config.usage.numberZapDigits.value == 1:
			self.Timer.start(100, True)
		if int(config.misc.zapkey_delay.value) > 0:
			self.Timer.start(int(1000 * int(config.misc.zapkey_delay.value)), True)


class InfoBarNumberZap:
	""" Handles an initial number for NumberZapping """

	def __init__(self):
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evStart: self.__serviceStarted,
			})
		def digitHelp():
			return _("Digit entry for service selection")
		self.toggleSeekStatus = False
		self["NumberActions"] = HelpableNumberActionMap(self, ["NumberActions"], {
			"1": (self.keyNumberGlobal, digitHelp()),
			"2": (self.keyNumberGlobal, digitHelp()),
			"3": (self.keyNumberGlobal, digitHelp()),
			"4": (self.keyNumberGlobal, digitHelp()),
			"5": (self.keyNumberGlobal, digitHelp()),
			"6": (self.keyNumberGlobal, digitHelp()),
			"7": (self.keyNumberGlobal, digitHelp()),
			"8": (self.keyNumberGlobal, digitHelp()),
			"9": (self.keyNumberGlobal, digitHelp()),
			"0": (self.keyNumberGlobal, digitHelp())
		}, prio=0, description=_("Service Selection Actions"))

	def __serviceStarted(self):
		self.toggleSeekStatus = False

	def toggleSeek(self):
		self.seekable = self.getSeek()
		if self.seekable:
			self.toggleSeekStatus = not self.toggleSeekStatus
			self.VideoMode_window.setText(_("Numberbuttons Seek") if self.toggleSeekStatus else _("Numberbuttons Zap"))

	def keyNumberGlobal(self, number):
		if number == 0:
			if isinstance(self, InfoBarPiP) and self.pipHandles0Action():
				self.pipDoHandle0Action()
			elif len(self.servicelist.history) > 1:
				self.checkTimeshiftRunning(self.recallPrevService)
		else:
			if self.toggleSeekStatus:
				length = self.seekable.getLength() or (None, 0)
				if length[1] > 0:
					key = int(number)
					time = (-config.seek.selfdefined_13.value, False, config.seek.selfdefined_13.value,
						-config.seek.selfdefined_46.value, False, config.seek.selfdefined_46.value,
						-config.seek.selfdefined_79.value, False, config.seek.selfdefined_79.value)[key - 1]
					self.seekable.seekRelative(time < 0 and -1 or 1, abs(time * 90000))
				return
			if "TimeshiftActions" in self and self.timeshiftEnabled():
				ts = self.getTimeshift()
				if ts and ts.isTimeshiftActive():
					return
			self.session.openWithCallback(self.numberEntered, NumberZap, number, self.searchNumber)

	def recallPrevService(self, reply):
		if reply:
			self.servicelist.recallPrevService()

	def numberEntered(self, service=None, bouquet=None):
		if service:
			self.selectAndStartService(service, bouquet)

	def searchNumberHelper(self, serviceHandler, num, bouquet):
		servicelist = serviceHandler.list(bouquet)
		if servicelist:
			serviceIterator = servicelist.getNext()
			while serviceIterator.valid():
				if num == serviceIterator.getChannelNum():
					return serviceIterator
				serviceIterator = servicelist.getNext()
		return None

	def searchNumber(self, number, firstBouquetOnly=False, bouquet=None):
		bouquet = bouquet or self.servicelist.getRoot()
		service = None
		serviceHandler = eServiceCenter.getInstance()
		if not firstBouquetOnly:
			service = self.searchNumberHelper(serviceHandler, number, bouquet)
		if config.usage.multibouquet.value and not service:
			bouquet = self.servicelist.bouquet_root
			bouquetlist = serviceHandler.list(bouquet)
			if bouquetlist:
				bouquet = bouquetlist.getNext()
				while bouquet.valid():
					if bouquet.flags & eServiceReference.isDirectory and not bouquet.flags & eServiceReference.isInvisible:
						service = self.searchNumberHelper(serviceHandler, number, bouquet)
						if service:
							playable = not (service.flags & (eServiceReference.isMarker | eServiceReference.isDirectory)) or (service.flags & eServiceReference.isNumberedMarker)
							if not playable:
								service = None
							break
						if config.usage.alternative_number_mode.value or firstBouquetOnly:
							break
					bouquet = bouquetlist.getNext()
		return service, bouquet

	def selectAndStartService(self, service, bouquet):
		if service and not service.flags & eServiceReference.isMarker:
			if self.servicelist.getRoot() != bouquet:  # already in correct bouquet?
				self.servicelist.clearPath()
				if self.servicelist.bouquet_root != bouquet:
					self.servicelist.enterPath(self.servicelist.bouquet_root)
				self.servicelist.enterPath(bouquet)
			self.servicelist.setCurrentSelection(service)  # select the service in servicelist
			self.servicelist.zap(enable_pipzap=True)
			self.servicelist.correctChannelNumber()
			self.servicelist.startRoot = None

	def zapToNumber(self, number):
		service, bouquet = self.searchNumber(number)
		self.selectAndStartService(service, bouquet)


config.misc.initialchannelselection = ConfigBoolean(default=True)


class InfoBarChannelSelection:
	""" ChannelSelection - handles the channelSelection dialog and the initial
	channelChange actions which open the channelSelection dialog """

	def __init__(self):
		# instantiate forever
		self.servicelist = self.session.instantiateDialog(ChannelSelection)

		if config.misc.initialchannelselection.value:
			self.onShown.append(self.firstRun)

		self["ChannelSelectActions"] = HelpableActionMap(self, ["InfobarChannelSelection"],
			{
				"keyUp": (self.keyUpCheck, self.getKeyUpHelptext),
				"keyDown": (self.keyDownCheck, self.getKeyDownHelpText),
				"keyLeft": (self.keyLeftCheck, self.getKeyLeftHelptext),
				"keyRight": (self.keyRightCheck, self.getKeyRightHelptext),
				"historyBack": (self.historyBack, _("Switch to previous channel in history")),
				"historyNext": (self.historyNext, _("Switch to next channel in history")),
				"keyChannelUp": (self.keyChannelUpCheck, self.getKeyChannelUpHelptext),
				"keyChannelDown": (self.keyChannelDownCheck, self.getKeyChannelDownHelptext),
			})

	def showTvChannelList(self, zap=False):
		self.servicelist.setModeTv()
		if zap:
			self.servicelist.zap()

	def showRadioChannelList(self, zap=False):
		self.servicelist.setModeRadio()
		if zap:
			self.servicelist.zap()

	def firstRun(self):
		self.servicelist.setMode()
		self.onShown.remove(self.firstRun)
		config.misc.initialchannelselection.value = False
		config.misc.initialchannelselection.save()
		self.switchChannelDown()

	def historyBack(self):
		self.checkTimeshiftRunning(self.historyBackCheckTimeshiftCallback)

	def historyBackCheckTimeshiftCallback(self, answer):
		if answer:
			if config.usage.historymode.getValue() == '0':
				self.servicelist.historyBack()
			else:
				self.servicelist.historyZap(-1)

	def historyNext(self):
		self.checkTimeshiftRunning(self.historyNextCheckTimeshiftCallback)

	def historyNextCheckTimeshiftCallback(self, answer):
		if answer:
			if config.usage.historymode.getValue() == '0':
				self.servicelist.historyNext()
			else:
				self.servicelist.historyZap(+1)

	def keyUpCheck(self):
		if config.usage.oldstyle_zap_controls.value:
			self.zapDown()
		elif config.usage.volume_instead_of_channelselection.value:
			self.volumeUp()
		else:
			self.switchChannelUp()

	def keyDownCheck(self):
		if config.usage.oldstyle_zap_controls.value:
			self.zapUp()
		elif config.usage.volume_instead_of_channelselection.value:
			self.volumeDown()
		else:
			self.switchChannelDown()

	def keyLeftCheck(self):
		if config.usage.oldstyle_zap_controls.value:
			if config.usage.volume_instead_of_channelselection.value:
				self.volumeDown()
			else:
				self.switchChannelUp()
		else:
			self.zapUp()

	def keyRightCheck(self):
		if config.usage.oldstyle_zap_controls.value:
			if config.usage.volume_instead_of_channelselection.value:
				self.volumeUp()
			else:
				self.switchChannelDown()
		else:
			self.zapDown()

	def keyChannelUpCheck(self):
		if config.usage.zap_with_ch_buttons.value:
			self.zapDown()
		else:
			self.openServiceList()

	def keyChannelDownCheck(self):
		if config.usage.zap_with_ch_buttons.value:
			self.zapUp()
		else:
			self.openServiceList()

	def getKeyUpHelptext(self):
		if config.usage.oldstyle_zap_controls.value:
			value = _("Switch to next channel")
		else:
			if config.usage.volume_instead_of_channelselection.value:
				value = _("Volume up")
			else:
				value = _("Open service list")
				if "keep" not in config.usage.servicelist_cursor_behavior.value:
					value += " " + _("and select previous channel")
		return value

	def getKeyDownHelpText(self):
		if config.usage.oldstyle_zap_controls.value:
			value = _("Switch to previous channel")
		else:
			if config.usage.volume_instead_of_channelselection.value:
				value = _("Volume down")
			else:
				value = _("Open service list")
				if "keep" not in config.usage.servicelist_cursor_behavior.value:
					value += " " + _("and select next channel")
		return value

	def getKeyLeftHelptext(self):
		if config.usage.oldstyle_zap_controls.value:
			if config.usage.volume_instead_of_channelselection.value:
				value = _("Volume down")
			else:
				value = _("Open service list")
				if "keep" not in config.usage.servicelist_cursor_behavior.value:
					value += " " + _("and select previous channel")
		else:
			value = _("Switch to previous channel")
		return value

	def getKeyRightHelptext(self):
		if config.usage.oldstyle_zap_controls.value:
			if config.usage.volume_instead_of_channelselection.value:
				value = _("Volume up")
			else:
				value = _("Open service list")
				if "keep" not in config.usage.servicelist_cursor_behavior.value:
					value += " " + _("and select next channel")
		else:
			value = _("Switch to next channel")
		return value

	def getKeyChannelUpHelptext(self):
		return config.usage.zap_with_ch_buttons.value and _("Switch to next channel") or _("Open service list")

	def getKeyChannelDownHelptext(self):
		return config.usage.zap_with_ch_buttons.value and _("Switch to previous channel") or _("Open service list")

	def switchChannelUp(self):
		if "keep" not in config.usage.servicelist_cursor_behavior.value:
			self.servicelist.moveUp()
		self.openServiceList()

	def switchChannelDown(self):
		if "keep" not in config.usage.servicelist_cursor_behavior.value:
			self.servicelist.moveDown()
		self.openServiceList()

	def zapUp(self):
		if self.servicelist.inBouquet():
			prev = self.servicelist.getCurrentSelection()
			if prev:
				prev = prev.toString()
				while True:
					if config.usage.quickzap_bouquet_change.value:
						if self.servicelist.atBegin():
							self.servicelist.prevBouquet()
					self.servicelist.moveUp()
					cur = self.servicelist.getCurrentSelection()
					if cur:
						if self.servicelist.dopipzap:
							isPlayable = self.session.pip.isPlayableForPipService(cur)
						else:
							isPlayable = isPlayableForCur(cur)
					if cur and (cur.toString() == prev or isPlayable):
							break
		else:
			self.servicelist.moveUp()
		self.servicelist.zap(enable_pipzap=True)

	def zapDown(self):
		if self.servicelist.inBouquet():
			prev = self.servicelist.getCurrentSelection()
			if prev:
				prev = prev.toString()
				while True:
					if config.usage.quickzap_bouquet_change.value and self.servicelist.atEnd():
						self.servicelist.nextBouquet()
					else:
						self.servicelist.moveDown()
					cur = self.servicelist.getCurrentSelection()
					if cur:
						if self.servicelist.dopipzap:
							isPlayable = self.session.pip.isPlayableForPipService(cur)
						else:
							isPlayable = isPlayableForCur(cur)
					if cur and (cur.toString() == prev or isPlayable):
							break
		else:
			self.servicelist.moveDown()
		self.servicelist.zap(enable_pipzap=True)

	def openFavouritesList(self):
		self.servicelist.showFavourites()
		self.openServiceList()

	def openSatellitesList(self):
		self.servicelist.showSatellites()
		self.openServiceList()

	def openServiceList(self):
		if config.skin.autorefresh.value:
			self.servicelist.servicelist.reloadSkin()
		self.session.execDialog(self.servicelist)

	def volumeUp(self):
		VolumeControl.instance and VolumeControl.instance.keyVolumeUp()

	def volumeDown(self):
		VolumeControl.instance and VolumeControl.instance.keyVolumeDown()


class InfoBarMenu:
	""" Handles a menu action, to open the (main) menu """

	def __init__(self):
		self["MenuActions"] = HelpableActionMap(self, ["InfobarMenuActions"],
			{
				"mainMenu": (self.mainMenu, _("Enter main menu...")),
			})
		self.session.infobar = None

	def mainMenu(self):
		print("[InfoBarGenerics] loading mainmenu XML...")
		menu = mdom.getroot()
		assert menu.tag == "menu", "root element in menu must be 'menu'!"

		self.session.infobar = self
		# so we can access the currently active infobar from screens opened from within the mainmenu
		# at the moment used from the SubserviceSelection

		self.session.openWithCallback(self.mainMenuClosed, MainMenu, menu)

	def mainMenuClosed(self, *val):
		self.session.infobar = None


class InfoBarSimpleEventView:
	""" Opens the Eventview for now/next """

	def __init__(self):
		self["EPGActions"] = HelpableActionMap(self, ["InfobarEPGActions"],
			{
				"showEventInfo": (self.openEventView, _("Show event details")),
				"showEventInfoSingleEPG": (self.openEventView, _("Show event details")),
				"showInfobarOrEpgWhenInfobarAlreadyVisible": self.showEventInfoWhenNotVisible,
			})

	def showEventInfoWhenNotVisible(self):
		if self.shown:
			self.openEventView()
		else:
			self.toggleShow()
			return 1

	def openEventView(self):
		epglist = []
		self.epglist = epglist
		service = self.session.nav.getCurrentService()
		ref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		info = service.info()
		ptr = info.getEvent(0)
		if ptr:
			epglist.append(ptr)
		ptr = info.getEvent(1)
		if ptr:
			epglist.append(ptr)
		if epglist:
			self.session.open(EventViewSimple, epglist[0], ServiceReference(ref), self.eventViewCallback)

	def eventViewCallback(self, setEvent, setService, val):  # used for now/next displaying
		epglist = self.epglist
		if len(epglist) > 1:
			tmp = epglist[0]
			epglist[0] = epglist[1]
			epglist[1] = tmp
			setEvent(epglist[0])


class SimpleServicelist:
	def __init__(self, services):
		self.setServices(services)

	def setServices(self, services):
		self.services = services
		self.length = len(services)
		self.current = 0

	def selectService(self, service):
		if not self.length:
			self.current = -1
			return False
		else:
			self.current = 0
			while self.services[self.current].ref != service:
				self.current += 1
				if self.current >= self.length:
					return False
		return True

	def nextService(self):
		if not self.length:
			return
		if self.current + 1 < self.length:
			self.current += 1
		else:
			self.current = 0

	def prevService(self):
		if not self.length:
			return
		if self.current - 1 > -1:
			self.current -= 1
		else:
			self.current = self.length - 1

	def currentService(self):
		if not self.length or self.current >= self.length:
			return None
		return self.services[self.current]


class InfoBarEPG:
	""" EPG - Opens an EPG list when the showEPGList action fires """

	def __init__(self):
		self.is_now_next = False
		self.dlg_stack = []
		self.bouquetSel = None
		self.eventView = None
		self.epglist = []
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evUpdatedEventInfo: self.__evEventInfoChanged,
			})

		self["EPGActions"] = HelpableActionMap(self, ["InfobarEPGActions"],
			{
				"showEventInfo": (self.showDefaultEPG, _("Show EPG...")),
				"showEventInfoSingleEPG": (self.showSingleEPG, _("Show single service EPG")),
				"showEventInfoMultiEPG": (self.showMultiEPG, _("Show multi channel EPG")),
				"showInfobarOrEpgWhenInfobarAlreadyVisible": self.showEventInfoWhenNotVisible,
			})

	def getEPGPluginList(self, getAll=False):
		pluginlist = [(p.name, boundFunction(self.runPlugin, p), p.description or p.name) for p in plugins.getPlugins(where=PluginDescriptor.WHERE_EVENTINFO)
				if 'selectedevent' not in p.fnc.__code__.co_varnames] or []
		from Components.ServiceEventTracker import InfoBarCount
		if getAll or InfoBarCount == 1:
			pluginlist.append((_("Show EPG for current channel..."), self.openSingleServiceEPG, _("Display EPG list for current channel")))
		pluginlist.append((_("Multi EPG"), self.openMultiServiceEPG, _("Display EPG as MultiEPG")))
		pluginlist.append((_("Current event EPG"), self.openEventView, _("Display EPG info for current event")))
		return pluginlist

	def showEventInfoWhenNotVisible(self):
		if self.shown:
			self.openEventView()
		else:
			self.toggleShow()
			return 1

	def zapToService(self, service, preview=False, zapback=False):
		if self.servicelist.startServiceRef is None:
			self.servicelist.startServiceRef = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if service is not None:
			if self.servicelist.getRoot() != self.epg_bouquet:  # already in correct bouquet?
				self.servicelist.clearPath()
				if self.servicelist.bouquet_root != self.epg_bouquet:
					self.servicelist.enterPath(self.servicelist.bouquet_root)
				self.servicelist.enterPath(self.epg_bouquet)
			self.servicelist.setCurrentSelection(service)  # select the service in servicelist
		if not zapback or preview:
			self.servicelist.zap(enable_pipzap=True)
		if (self.servicelist.dopipzap or zapback) and not preview:
			self.servicelist.zapBack()
		if not preview:
			self.servicelist.startServiceRef = None
			self.servicelist.startRoot = None

	def getBouquetServices(self, bouquet):
		services = []
		servicelist = eServiceCenter.getInstance().list(bouquet)
		if not servicelist is None:
			while True:
				service = servicelist.getNext()
				if not service.valid():  # check if end of list
					break
				if service.flags & (eServiceReference.isDirectory | eServiceReference.isMarker):  # ignore non playable services
					continue
				services.append(ServiceReference(service))
		return services

	def openBouquetEPG(self, bouquet, withCallback=True):
		services = self.getBouquetServices(bouquet)
		if services:
			self.epg_bouquet = bouquet
			if withCallback:
				self.dlg_stack.append(self.session.openWithCallback(self.closed, EPGSelection, services, self.zapToService, None, self.changeBouquetCB))
			else:
				self.session.open(EPGSelection, services, self.zapToService, None, self.changeBouquetCB)

	def changeBouquetCB(self, direction, epg):
		if self.bouquetSel:
			if direction > 0:
				self.bouquetSel.down()
			else:
				self.bouquetSel.up()
			bouquet = self.bouquetSel.getCurrent()
			services = self.getBouquetServices(bouquet)
			if services:
				self.epg_bouquet = bouquet
				epg.setServices(services)

	def selectBouquet(self, bouquetref, epg):
		services = self.getBouquetServices(bouquetref)
		if services:
			self.epg_bouquet = bouquetref
			self.serviceSel.setServices(services)
			epg.setServices(services)

	def setService(self, service):
		if service:
			self.serviceSel.selectService(service)

	def closed(self, ret=False):
		if not self.dlg_stack:
			return
		closedScreen = self.dlg_stack.pop()
		if self.bouquetSel and closedScreen == self.bouquetSel:
			self.bouquetSel = None
		elif self.eventView and closedScreen == self.eventView:
			self.eventView = None
		if ret is True or ret == "close":
			dlgs = len(self.dlg_stack)
			if dlgs > 0:
				self.dlg_stack[dlgs - 1].close(dlgs > 1)

	def openMultiServiceEPG(self, withCallback=True):
		bouquets = self.servicelist.getBouquetList()
		if bouquets is None:
			cnt = 0
		else:
			cnt = len(bouquets)
		if config.usage.multiepg_ask_bouquet.value:
			self.openMultiServiceEPGAskBouquet(bouquets, cnt, withCallback)
		else:
			self.openMultiServiceEPGSilent(bouquets, cnt, withCallback)

	def openMultiServiceEPGAskBouquet(self, bouquets, cnt, withCallback):
		if cnt > 1:  # show bouquet list
			if withCallback:
				self.bouquetSel = self.session.openWithCallback(self.closed, BouquetSelector, bouquets, self.openBouquetEPG, enableWrapAround=True)
				self.dlg_stack.append(self.bouquetSel)
			else:
				self.bouquetSel = self.session.open(BouquetSelector, bouquets, self.openBouquetEPG, enableWrapAround=True)
		elif cnt == 1:
			self.openBouquetEPG(bouquets[0][1], withCallback)

	def openMultiServiceEPGSilent(self, bouquets, cnt, withCallback):
		root = self.servicelist.getRoot()
		rootstr = root.toCompareString()
		current = 0
		for bouquet in bouquets:
			if bouquet[1].toCompareString() == rootstr:
				break
			current += 1
		if current >= cnt:
			current = 0
		if cnt > 1:  # create bouquet list for bouq+/-
			self.bouquetSel = SilentBouquetSelector(bouquets, True, self.servicelist.getBouquetNumOffset(root))
		if cnt >= 1:
			self.openBouquetEPG(root, withCallback)

	def changeServiceCB(self, direction, epg):
		if self.serviceSel:
			if direction > 0:
				self.serviceSel.nextService()
			else:
				self.serviceSel.prevService()
			epg.setService(self.serviceSel.currentService())

	def SingleServiceEPGClosed(self, ret=False):
		self.serviceSel = None

	def openSingleServiceEPG(self):
		ref = self.servicelist.getCurrentSelection()
		if ref:
			if self.servicelist.getMutableList():  # bouquet in channellist
				current_path = self.servicelist.getRoot()
				services = self.getBouquetServices(current_path)
				self.serviceSel = SimpleServicelist(services)
				if self.serviceSel.selectService(ref):
					self.epg_bouquet = current_path
					self.session.openWithCallback(self.SingleServiceEPGClosed, EPGSelection, ref, self.zapToService, serviceChangeCB=self.changeServiceCB, parent=self)
				else:
					self.session.openWithCallback(self.SingleServiceEPGClosed, EPGSelection, ref)
			else:
				self.session.open(EPGSelection, ref)

	def runPlugin(self, plugin):
		plugin(session=self.session, servicelist=self.servicelist)

	def showEventInfoPlugins(self):
		pluginlist = self.getEPGPluginList()
		if pluginlist:
			self.session.openWithCallback(self.EventInfoPluginChosen, ChoiceBox, title=_("Please choose an extension..."), list=pluginlist, skin_name="EPGExtensionsList", reorderConfig="eventinfoOrder", windowTitle=_("Events info menu"))
		else:
			self.openSingleServiceEPG()

	def EventInfoPluginChosen(self, answer):
		if answer is not None:
			answer[1]()

	def openSimilarList(self):
		id = self.event and self.event.getEventId()
		refstr = str(self.currentService)
		if id is not None:
			self.hide()
			self.secondInfoBarWasShown = False
			self.session.open(EPGSelection, refstr, None, id)

	def getNowNext(self):
		epglist = []
		service = self.session.nav.getCurrentService()
		info = service and service.info()
		ptr = info and info.getEvent(0)
		if ptr and ptr.getEventName() != "":
			epglist.append(ptr)
		ptr = info and info.getEvent(1)
		if ptr and ptr.getEventName() != "":
			epglist.append(ptr)
		self.epglist = epglist

	def __evEventInfoChanged(self):
		if self.is_now_next and len(self.dlg_stack) == 1:
			self.getNowNext()
			if self.eventView and self.epglist:
				self.eventView.setEvent(self.epglist[0])

	def showDefaultEPG(self):
		self.openEventView()

	def showSingleEPG(self):
		self.openSingleServiceEPG()

	def showMultiEPG(self):
		self.openMultiServiceEPG()

	def openEventView(self):
		from Components.ServiceEventTracker import InfoBarCount
		if InfoBarCount > 1:
			epglist = []
			self.epglist = epglist
			service = self.session.nav.getCurrentService()
			ref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
			info = service.info()
			ptr = info.getEvent(0)
			if ptr:
				epglist.append(ptr)
			ptr = info.getEvent(1)
			if ptr:
				epglist.append(ptr)
			if epglist:
				self.session.open(EventViewEPGSelect, epglist[0], ServiceReference(ref), self.eventViewCallback, self.openSingleServiceEPG, self.openMultiServiceEPG, self.openSimilarList)
		else:
			ref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
			self.getNowNext()
			epglist = self.epglist
			if not epglist:
				self.is_now_next = False
				epg = eEPGCache.getInstance()
				ptr = ref and ref.valid() and epg.lookupEventTime(ref, -1)
				if ptr:
					epglist.append(ptr)
					ptr = epg.lookupEventTime(ref, ptr.getBeginTime(), +1)
					if ptr:
						epglist.append(ptr)
			else:
				self.is_now_next = True
			if epglist:
				self.eventView = self.session.openWithCallback(self.closed, EventViewEPGSelect, epglist[0], ServiceReference(ref), self.eventViewCallback, self.openSingleServiceEPG, self.openMultiServiceEPG, self.openSimilarList)
				self.dlg_stack.append(self.eventView)
		if not epglist:
			print("[InfoBarGenerics] no epg for the service avail.. so we show multiepg instead of eventinfo")
			self.openMultiServiceEPG(False)

	def eventViewCallback(self, setEvent, setService, val):  # used for now/next displaying
		epglist = self.epglist
		if len(epglist) > 1:
			tmp = epglist[0]
			epglist[0] = epglist[1]
			epglist[1] = tmp
			setEvent(epglist[0])


class InfoBarRdsDecoder:
	"""provides RDS and Rass support/display"""

	def __init__(self):
		self.rds_display = self.session.instantiateDialog(RdsInfoDisplay)
		self.session.instantiateSummaryDialog(self.rds_display)
		self.rds_display.setAnimationMode(0)
		self.rass_interactive = None

		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evEnd: self.__serviceStopped,
				iPlayableService.evUpdatedRassSlidePic: self.RassSlidePicChanged
			})

		self["RdsActions"] = ActionMap(["InfobarRdsActions"],
		{
			"startRassInteractive": self.startRassInteractive
		}, -1)

		self["RdsActions"].setEnabled(False)

		self.onLayoutFinish.append(self.rds_display.show)
		self.rds_display.onRassInteractivePossibilityChanged.append(self.RassInteractivePossibilityChanged)

	def RassInteractivePossibilityChanged(self, state):
		self["RdsActions"].setEnabled(state)

	def RassSlidePicChanged(self):
		if not self.rass_interactive:
			service = self.session.nav.getCurrentService()
			decoder = service and service.rdsDecoder()
			if decoder:
				decoder.showRassSlidePicture()

	def __serviceStopped(self):
		if self.rass_interactive is not None:
			rass_interactive = self.rass_interactive
			self.rass_interactive = None
			rass_interactive.close()

	def startRassInteractive(self):
		self.rds_display.hide()
		self.rass_interactive = self.session.openWithCallback(self.RassInteractiveClosed, RassInteractive)

	def RassInteractiveClosed(self, *val):
		if self.rass_interactive is not None:
			self.rass_interactive = None
			self.RassSlidePicChanged()
		self.rds_display.show()


class SeekBar(Screen):
	skin = """
	<screen name="SeekBar" position="center,10" size="800,50" flags="wfNoBorder" resolution="1280,720">
		<widget name="target" position="10,10" size="100,20" font="Regular;20" horizontalAlignment="right" transparent="1" verticalAlignment="center" />
		<widget source="session.CurrentService" render="PositionGauge" position="120,10" size="e-240,20" foregroundColor="#000000CF" transparent="1">
			<convert type="ServicePosition">Gauge</convert>
		</widget>
		<widget source="session.CurrentService" render="Progress" position="120,18" size="e-240,4" backgroundColor="#000000CF" borderWidth="0" foregroundColor="#0000CF00" zPosition="+1">
			<convert type="ServicePosition">Position</convert>
		</widget>
		<widget name="cursor" position="0,0" size="8,18" pixmap="sliders/position_arrow.png" alphatest="blend" zPosition="+2" />
		<widget name="length" position="e-110,10" size="100,20" font="Regular;20" transparent="1" verticalAlignment="center" />
	</screen>"""

	ARROW_TRADITIONAL = "t"
	ARROW_SYMMETRICAL = "s"
	ARROW_DEFINED = "d"
	SKIP_SYMMETRICAL = "s"
	SKIP_DEFINED = "d"
	SKIP_PERCENTAGE = "p"

	def __init__(self, session):
		def sensibilityHelp(button):
			match button:
				case "UP":
					helpText = _("Skip forward %s%%") % f"{config.seek.sensibilityVertical.value:.1f}"
				case "LEFT":
					helpText = _("Skip backward %s%%") % f"{config.seek.sensibilityHorizontal.value:.1f}"
				case "RIGHT":
					helpText = _("Skip forward %s%%") % f"{config.seek.sensibilityHorizontal.value:.1f}"
				case "DOWN":
					helpText = _("Skip backward %s%%") % f"{config.seek.sensibilityVertical.value:.1f}"
			return helpText

		def symmetricalHelp(button):
			match button:
				case 1 | 3:
					value = config.seek.defined[13].value
				case 4 | 6:
					value = config.seek.defined[46].value
				case 7 | 9:
					value = config.seek.defined[79].value
			helpText = (ngettext("Skip backward %d second", "Skip backward %d seconds", value) if button % 3 else ngettext("Skip forward %d second", "Skip forward %d seconds", value)) % value
			return helpText

		def definedHelp(button):
			value = config.seek.defined[button].value
			if value < 0:
				value = abs(value)
				helpText = ngettext("Skip backward %d second", "Skip backward %d seconds", value) % value
			elif value > 0:
				helpText = ngettext("Skip forward %d second", "Skip forward %d seconds", value) % value
			else:
				helpText = _("Skip for '%s' is disabled") % button
			return helpText

		def percentageHelp(button):
			if button:
				helpText = _("Skip to %s0%% position (Add %s%% on second press)") % (button, button)
			else:
				# helpText = _("Skip to 0% (start) position (Skip to 100% on second press)")  # Make 00 equal to 100%.
				helpText = _("Skip to 0% (start) position")
			return helpText

		Screen.__init__(self, session, mandatoryWidgets=["length"], enableHelp=True)
		self.setTitle(_("Seek Bar"))
		self["target"] = Label()
		self["cursor"] = MovingPixmap()
		self["length"] = Label()
		self["actions"] = HelpableActionMap(self, ["OkCancelActions"], {
			"ok": (self.keyOK, "Close the SeekBar at the current location"),
			"cancel": (self.keyCancel, _("Close the SeekBar after returning to the starting point"))
		}, prio=0, description=_("SeekBar Actions"))
		match config.seek.arrowSkipMode.value:
			case self.ARROW_TRADITIONAL:
				self["arrowSeekActions"] = HelpableActionMap(self, ["NavigationActions"], {
					"left": (self.keyLeft, boundFunction(sensibilityHelp, "LEFT")),
					"right": (self.keyRight, boundFunction(sensibilityHelp, "RIGHT"))
				}, prio=0, description=_("SeekBar Actions"))
			case self.ARROW_SYMMETRICAL:
				self["arrowSeekActions"] = HelpableActionMap(self, ["NavigationActions"], {
					"up": (self.keyUp, boundFunction(sensibilityHelp, "UP")),
					"left": (self.keyLeft, boundFunction(sensibilityHelp, "LEFT")),
					"right": (self.keyRight, boundFunction(sensibilityHelp, "RIGHT")),
					"down": (self.keyDown, boundFunction(sensibilityHelp, "DOWN"))
				}, prio=0, description=_("SeekBar Actions"))
			case self.ARROW_DEFINED:
				self["arrowSeekActions"] = HelpableActionMap(self, ["NavigationActions"], {
					"up": (self.keyUp, boundFunction(definedHelp, "UP")),
					"left": (self.keyLeft, boundFunction(definedHelp, "LEFT")),
					"right": (self.keyRight, boundFunction(definedHelp, "RIGHT")),
					"down": (self.keyDown, boundFunction(definedHelp, "DOWN"))
				}, prio=0, description=_("SeekBar Actions"))
		match config.seek.numberSkipMode.value:
			case self.SKIP_SYMMETRICAL:
				self["numberSeekActions"] = HelpableNumberActionMap(self, ["NumberActions"], {
					"1": (self.keyNumberGlobal, boundFunction(symmetricalHelp, 1)),
					"3": (self.keyNumberGlobal, boundFunction(symmetricalHelp, 3)),
					"4": (self.keyNumberGlobal, boundFunction(symmetricalHelp, 4)),
					"6": (self.keyNumberGlobal, boundFunction(symmetricalHelp, 6)),
					"7": (self.keyNumberGlobal, boundFunction(symmetricalHelp, 7)),
					"9": (self.keyNumberGlobal, boundFunction(symmetricalHelp, 9))
				}, prio=0, description=_("SeekBar Actions"))
			case self.SKIP_DEFINED:
				self["numberSeekActions"] = HelpableNumberActionMap(self, ["NumberActions"], {
					"1": (self.keyNumberGlobal, boundFunction(definedHelp, 1)),
					"2": (self.keyNumberGlobal, boundFunction(definedHelp, 2)),
					"3": (self.keyNumberGlobal, boundFunction(definedHelp, 3)),
					"4": (self.keyNumberGlobal, boundFunction(definedHelp, 4)),
					"5": (self.keyNumberGlobal, boundFunction(definedHelp, 5)),
					"6": (self.keyNumberGlobal, boundFunction(definedHelp, 6)),
					"7": (self.keyNumberGlobal, boundFunction(definedHelp, 7)),
					"8": (self.keyNumberGlobal, boundFunction(definedHelp, 8)),
					"9": (self.keyNumberGlobal, boundFunction(definedHelp, 9)),
					"0": (self.keyNumberGlobal, boundFunction(definedHelp, 0))
				}, prio=0, description=_("SeekBar Actions"))
			case self.SKIP_PERCENTAGE:
				self["numberSeekActions"] = HelpableNumberActionMap(self, ["NumberActions"], {
					"1": (self.keyNumberGlobal, percentageHelp(1)),
					"2": (self.keyNumberGlobal, percentageHelp(2)),
					"3": (self.keyNumberGlobal, percentageHelp(3)),
					"4": (self.keyNumberGlobal, percentageHelp(4)),
					"5": (self.keyNumberGlobal, percentageHelp(5)),
					"6": (self.keyNumberGlobal, percentageHelp(6)),
					"7": (self.keyNumberGlobal, percentageHelp(7)),
					"8": (self.keyNumberGlobal, percentageHelp(8)),
					"9": (self.keyNumberGlobal, percentageHelp(9)),
					"0": (self.keyNumberGlobal, percentageHelp(0))
				}, prio=0, description=_("SeekBar Actions"))
		self.seekable = False
		service = session.nav.getCurrentService()
		serviceReference = self.session.nav.getCurrentlyPlayingServiceReference()
		if service and serviceReference:
			self.seek = service.seek()
			if not self.seek:
				print("[InfoBarGenerics] SeekBar: The current service does not support seeking!")
				self.close()
			if self.seek.isCurrentlySeekable() in (1, 3) and serviceReference.type == 1:  # 0=Not seek-able, 1=Blu-ray, 3=Fully seek-able. Type == 1 solves an issue in GStreamer where all media is always seek-able!
				self.seekable = True
		else:
			print("[InfoBarGenerics] SeekBar: There is no current service so there is nothing to seek!")
			self.close()
		self.length = self.seek.getLength()[1] if serviceReference.getPath() else None
		self.eventTracker = ServiceEventTracker(screen=self, eventmap={
			iPlayableService.evEOF: self.endOfFile
		})
		self.gaugeX = 0
		self.gaugeY = 0
		self.gaugeW = 0
		self.gaugeH = 0
		self.cursorW = 0
		self.cursorH = 0
		self.cursorTimer = eTimer()
		self.cursorTimer.callback.append(self.updateCursor)
		self.cursorTimer.start(250)  # This is a auto repeating timer to update the UI.
		self.target = self.seek.getPlayPosition()[1]  # Set initial target position to the current media position.
		self.start = self.target if self.seekable else None  # Remember the start position if we allow immediate media seeking.
		self.digitTime = 0.0
		self.firstDigit = True
		self.onShown.append(self.screenShown)

	def endOfFile(self):
		self.cursorTimer.stop()
		print("[InfoBarGenerics] SeekBar: The SeekBar playback has reached the end of file, exiting.")
		self.close()

	def updateCursor(self):
		length = self.seek.getLength()[1] if self.length is None else self.length
		target = self.target // 90000
		self["target"].setText(f"{target // 60}:{target % 60:02d}")
		cursorX = self.gaugeX + (self.target * self.gaugeW / length) - self.cursorC
		self["cursor"].moveTo(cursorX, self.cursorY, 1)
		self["cursor"].startMoving()
		length //= 90000
		self["length"].setText(f"{length // 60}:{length % 60:02d}")

	def screenShown(self):
		for component in self.active_components:
			if isinstance(component, (PositionGauge, Progress)):  # Progress is used for composite widgets like in Metrix.
				for attribute, value in component.skinAttributes:
					match attribute:
						case "position":
							self.gaugeX = value[0]
							self.gaugeY = value[1]
						case "size":
							self.gaugeW = value[0]
							self.gaugeH = value[1]
				break
		for attribute, value in self["cursor"].skinAttributes:
			if attribute == "size":
				self.cursorW = value[0]
				self.cursorH = value[1]
				self.cursorC = (self.cursorW - 1) // 2
				self.cursorY = self.gaugeY + (self.gaugeH // 2) - (self.cursorH // 2)
				break

	def keyOK(self):
		self.cursorTimer.stop()
		self.seek.seekTo(self.target)
		self.close()

	def keyCancel(self):
		self.cursorTimer.stop()
		if self.seekable and self.start is not None:  # Restore the initial media position if we allowed immediate media seeking.
			self.seek.seekTo(self.start)
		self.close()

	def keyUp(self):
		self.target = self.sensibilityTarget(1, config.seek.sensibilityVertical.value) if config.seek.arrowSkipMode.value == "s" else self.updateTarget(config.seek.defined["UP"].value)

	def keyLeft(self):
		self.target = self.sensibilityTarget(-1, config.seek.sensibilityHorizontal.value) if config.seek.arrowSkipMode.value == "s" else self.updateTarget(config.seek.defined["LEFT"].value)

	def keyRight(self):
		self.target = self.sensibilityTarget(1, config.seek.sensibilityHorizontal.value) if config.seek.arrowSkipMode.value == "s" else self.updateTarget(config.seek.defined["RIGHT"].value)

	def keyDown(self):
		self.target = self.sensibilityTarget(-1, config.seek.sensibilityVertical.value) if config.seek.arrowSkipMode.value == "s" else self.updateTarget(config.seek.defined["DOWN"].value)

	def keyNumberGlobal(self, number):
		match config.seek.numberSkipMode.value:
			case self.SKIP_SYMMETRICAL:
				match number:
					case 1 | 3:
						skip = config.seek.defined[13].value
					case 4 | 6:
						skip = config.seek.defined[46].value
					case 7 | 9:
						skip = config.seek.defined[79].value
				direction = -1 if number % 3 else 1
				self.target = self.updateTarget(skip * direction)
			case self.SKIP_DEFINED:
				self.target = self.updateTarget(config.seek.defined[number].value)
			case self.SKIP_PERCENTAGE:
				now = time()
				if now - self.digitTime >= 1.0:  # Second percentage digit must be pressed within 1 second else data entry resets.
					self.firstDigit = True
				self.digitTime = now
				length = self.seek.getLength()[1] if self.length is None else self.length
				if self.firstDigit:
					self.firstDigit = False
					self.target = 0
					self.target = self.updateTarget(float(length * number * 10) / 9000000.0)
				else:
					self.firstDigit = True
					# if number == 0:  # Make 00 equal to 100%.
					# 	number = 100
					self.target = self.updateTarget(float(length * number) / 9000000.0)

	def sensibilityTarget(self, direction, sensibility):
		self.firstDigit = True
		length = self.seek.getLength()[1] if self.length is None else self.length
		skip = (direction * length * sensibility / 100.0) / 90000.0
		return self.updateTarget(skip)

	def updateTarget(self, skip):
		target = self.target + int(skip * 90000)
		if target < 0:
			target = 0
		length = self.seek.getLength()[1] if self.length is None else self.length
		if target >= length:
			self.endOfFile()
		if self.seekable:
			self.seek.seekTo(target)
		return target


class InfoBarSeek:
	"""handles actions like seeking, pause"""

	SEEK_STATE_PLAY = (0, 0, 0, ">")
	SEEK_STATE_PAUSE = (1, 0, 0, "||")
	SEEK_STATE_EOF = (1, 0, 0, "END")

	def __init__(self, actionmap="InfobarSeekActions"):
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
			iPlayableService.evSeekableStatusChanged: self.__seekableStatusChanged,
			iPlayableService.evStart: self.__serviceStarted,
			iPlayableService.evEOF: self.__evEOF,
			iPlayableService.evSOF: self.__evSOF,
		})
		self.fast_winding_hint_message_showed = False

		class InfoBarSeekActionMap(HelpableActionMap):
			def __init__(self, screen, *args, **kwargs):
				HelpableActionMap.__init__(self, screen, *args, **kwargs)
				self.screen = screen

			def action(self, contexts, action):
				# print("[InfoBarGenerics] action:", action)
				if action[:5] == "seek:":
					time = int(action[5:])
					self.screen.doSeekRelative(time * 90000)
					return 1
				elif action[:8] == "seekdef:":
					key = int(action[8:])
					time = (-config.seek.selfdefined_13.value, False, config.seek.selfdefined_13.value,
						-config.seek.selfdefined_46.value, False, config.seek.selfdefined_46.value,
						-config.seek.selfdefined_79.value, False, config.seek.selfdefined_79.value)[key - 1]
					self.screen.doSeekRelative(time * 90000)
					return 1
				else:
					return HelpableActionMap.action(self, contexts, action)

		self["SeekActions"] = InfoBarSeekActionMap(self, actionmap, {
			"playpauseService": (self.playpauseService, _("Pause/Continue playback")),
			"pauseService": (self.pauseService, _("Pause playback")),
			"pauseServiceYellow": (self.pauseServiceYellow, _("Pause playback")),
			"unPauseService": (self.unPauseService, _("Continue playback")),
			"okButton": (self.okButton, _("Continue playback")),
			"seekFwd": (self.seekFwd, _("Seek forward")),
			"seekFwdManual": (self.seekFwdManual, _("Seek forward (enter time)")),
			"seekBack": (self.seekBack, _("Seek backward")),
			"seekBackManual": (self.seekBackManual, _("Seek backward (enter time)")),
			"SeekbarFwd": self.seekFwdSeekbar,
			"SeekbarBack": self.seekBackSeekbar
		}, prio=-1, description=_("Seek Actions"))  # Give them a little more priority to win over the color buttons.
		self["SeekActions"].setEnabled(False)
		self.activity = 0
		self.activityTimer = eTimer()
		self.activityTimer.callback.append(self.doActivityTimer)
		self.seekstate = self.SEEK_STATE_PLAY
		self.lastseekstate = self.SEEK_STATE_PLAY
		self.seekAction = 0
		self.LastseekAction = False
		self.onPlayStateChanged = []
		self.lockedBecauseOfSkipping = False
		self.__seekableStatusChanged()

	def makeStateForward(self, n):
		return 0, n, 0, ">> %dx" % n

	def makeStateBackward(self, n):
		return 0, -n, 0, "<< %dx" % n

	def makeStateSlowMotion(self, n):
		return 0, 0, n, "/%d" % n

	def isStateForward(self, state):
		return state[1] > 1

	def isStateBackward(self, state):
		return state[1] < 0

	def isStateSlowMotion(self, state):
		return state[1] == 0 and state[2] > 1

	def getHigher(self, n, lst):
		for x in lst:
			if x > n:
				return x
		return False

	def getLower(self, n, lst):
		lst = lst[:]
		lst.reverse()
		for x in lst:
			if x < n:
				return x
		return False

	def showAfterSeek(self):
		if isinstance(self, InfoBarShowHide):
			self.doShow()

	def up(self):
		pass

	def down(self):
		pass

	def getSeek(self):
		service = self.session.nav.getCurrentService()
		if service is None:
			return None
		seek = service.seek()
		if seek is None or not seek.isCurrentlySeekable():
			return None
		return seek

	def isSeekable(self):
		if self.getSeek() is None or (isStandardInfoBar(self) and not self.timeshiftEnabled()):
			return False
		return True

	def __seekableStatusChanged(self):
		if isStandardInfoBar(self) and self.timeshiftEnabled():
			pass
		elif not self.isSeekable():
			BoxInfo.setMutableItem("SeekStatePlay", False)
			if exists("/proc/stb/lcd/symbol_hdd"):
				f = open("/proc/stb/lcd/symbol_hdd", "w")
				f.write("0")
				f.close()
			if exists("/proc/stb/lcd/symbol_hddprogress"):
				f = open("/proc/stb/lcd/symbol_hddprogress", "w")
				f.write("0")
				f.close()
			# print("not seekable, return to play")
			self["SeekActions"].setEnabled(False)
			self.setSeekState(self.SEEK_STATE_PLAY)
		else:
			# print("seekable")
			self["SeekActions"].setEnabled(True)
			self.activityTimer.start(int(config.seek.withjumps_repeat_ms.getValue()), False)
			for c in self.onPlayStateChanged:
				c(self.seekstate)
		# global seek_withjumps_muted
		# if seek_withjumps_muted and eDVBVolumecontrol.getInstance().isMuted(True):
		# 	print("[InfoBarGenerics] STILL MUTED AFTER FFWD/FBACK !!!!!!!! so we unMute")
		# 	seek_withjumps_muted = False
		# 	eDVBVolumecontrol.getInstance().volumeUnMute()

	def doActivityTimer(self):
		if self.isSeekable():
			self.activity += 16
			hdd = 1
			if self.activity >= 100:
				self.activity = 0
			BoxInfo.setMutableItem("SeekStatePlay", True)
			if exists("/proc/stb/lcd/symbol_hdd"):
				if config.lcd.hdd.value:
					file = open("/proc/stb/lcd/symbol_hdd", "w")
					file.write("%d" % int(hdd))
					file.close()
			if exists("/proc/stb/lcd/symbol_hddprogress"):
				if config.lcd.hdd.value:
					file = open("/proc/stb/lcd/symbol_hddprogress", "w")
					file.write("%d" % int(self.activity))
					file.close()
		else:
			self.activityTimer.stop()
			self.activity = 0
			hdd = 0
			self.seekAction = 0
		BoxInfo.setMutableItem("SeekStatePlay", True)
		if exists("/proc/stb/lcd/symbol_hdd"):
			if config.lcd.hdd.value:
				file = open("/proc/stb/lcd/symbol_hdd", "w")
				file.write("%d" % int(hdd))
				file.close()
		if exists("/proc/stb/lcd/symbol_hddprogress"):
			if config.lcd.hdd.value:
				file = open("/proc/stb/lcd/symbol_hddprogress", "w")
				file.write("%d" % int(self.activity))
				file.close()
		if self.LastseekAction:
			self.DoSeekAction()

	def __serviceStarted(self):
		self.fast_winding_hint_message_showed = False
		self.setSeekState(self.SEEK_STATE_PLAY)
		self.__seekableStatusChanged()

	def setSeekState(self, state):
		service = self.session.nav.getCurrentService()
		if service is None:
			return False
		if not self.isSeekable():
			if state not in (self.SEEK_STATE_PLAY, self.SEEK_STATE_PAUSE):
				state = self.SEEK_STATE_PLAY
		pauseable = service.pause()
		if pauseable is None:
			# print("not pauseable.")
			state = self.SEEK_STATE_PLAY
		self.seekstate = state
		if pauseable is not None:
			if self.seekstate[0] and self.seekstate[3] == "||":
				# print("resolved to PAUSE")
				self.activityTimer.stop()
				pauseable.pause()
			elif self.seekstate[0] and self.seekstate[3] == "END":
				# print("resolved to STOP")
				self.activityTimer.stop()
			elif self.seekstate[1]:
				if not pauseable.setFastForward(self.seekstate[1]):
					pass
					# print("resolved to FAST FORWARD")
				else:
					self.seekstate = self.SEEK_STATE_PLAY
					# print("FAST FORWARD not possible: resolved to PLAY")
			elif self.seekstate[2]:
				if not pauseable.setSlowMotion(self.seekstate[2]):
					pass
					# print("resolved to SLOW MOTION")
				else:
					self.seekstate = self.SEEK_STATE_PAUSE
					# print("SLOW MOTION not possible: resolved to PAUSE")
			else:
				# print("resolved to PLAY")
				self.activityTimer.start(int(config.seek.withjumps_repeat_ms.getValue()), False)
				pauseable.unpause()
		for c in self.onPlayStateChanged:
			c(self.seekstate)
		self.checkSkipShowHideLock()
		if hasattr(self, "screenSaverTimerStart"):
			self.screenSaverTimerStart()
		return True

	def okButton(self):
		if self.seekstate == self.SEEK_STATE_PLAY:
			return 0
		elif self.seekstate == self.SEEK_STATE_PAUSE:
			self.pauseService()
		else:
			self.unPauseService()

	def playpauseService(self):
		if self.seekAction != 0:
			self.seekAction = 0
			self.doPause(False)
			# global seek_withjumps_muted
			# seek_withjumps_muted = False
			return
		if self.seekstate == self.SEEK_STATE_PLAY:
			self.pauseService()
		else:
			if self.seekstate == self.SEEK_STATE_PAUSE:
				if config.seek.on_pause.value == "play":
					self.unPauseService()
				elif config.seek.on_pause.value == "step":
					self.doSeekRelative(1)
				elif config.seek.on_pause.value == "last":
					self.setSeekState(self.lastseekstate)
					self.lastseekstate = self.SEEK_STATE_PLAY
			else:
				self.unPauseService()

	def pauseService(self):
		BoxInfo.setMutableItem("StatePlayPause", True)
		if self.seekstate != self.SEEK_STATE_EOF:
			self.lastseekstate = self.seekstate
		self.setSeekState(self.SEEK_STATE_PAUSE)

	def pauseServiceYellow(self):
		self.audioSelection()

	def unPauseService(self):
		BoxInfo.setMutableItem("StatePlayPause", False)
		if self.seekstate == self.SEEK_STATE_PLAY:
			if self.seekAction != 0:
				self.playpauseService()
			# return 0  # If 'return 0', plays time shift again from the beginning.
			return
		self.doPause(False)
		self.setSeekState(self.SEEK_STATE_PLAY)
		if config.usage.show_infobar_on_skip.value and not config.usage.show_infobar_locked_on_pause.value:
			self.showAfterSeek()
		self.skipToggleShow = True  # Skip 'break' action (toggleShow) after 'make' action (unPauseService).

	def doPause(self, pause):
		if pause:
			if not eDVBVolumecontrol.getInstance().isMuted(True):
				eDVBVolumecontrol.getInstance().volumeMute()
		else:
			if eDVBVolumecontrol.getInstance().isMuted(True):
				eDVBVolumecontrol.getInstance().volumeUnMute()

	def doSeek(self, pts):
		seekable = self.getSeek()
		if seekable is None:
			return
		seekable.seekTo(pts)

	def doSeekRelativeAvoidStall(self, pts):
		global jump_pts_adder
		global jump_last_pts
		global jump_last_pos
		seekable = self.getSeek()
		# When config.seek.withjumps, avoid that jumps smaller than the time between I-frames result in hanging, by increasing pts when stalled.
		if seekable and config.seek.withjumps_avoid_zero.getValue():
			position = seekable.getPlayPosition()
			if jump_last_pos and jump_last_pts:
				if (abs(position[1] - jump_last_pos[1]) < 100 * 90) and (pts == jump_last_pts):  # Stalled?
					jump_pts_adder += pts
					jump_last_pts = pts
					pts += jump_pts_adder
				else:
					jump_pts_adder = 0
					jump_last_pts = pts
			else:
				jump_last_pts = pts
			jump_last_pos = position
		self.doSeekRelative(pts)

	def doSeekRelative(self, pts):
		try:
			if "<class 'Screens.InfoBar.InfoBar'>" in repr(self):
				if InfoBarTimeshift.timeshiftEnabled(self):
					length = InfoBarTimeshift.ptsGetLength(self)
					position = InfoBarTimeshift.ptsGetPosition(self)
					if length is None or position is None:
						return
					if position + pts >= length:
						InfoBarTimeshift.evEOF(self, position + pts - length)
						self.showAfterSeek()
						return
					elif position + pts < 0:
						InfoBarTimeshift.evSOF(self, position + pts)
						self.showAfterSeek()
						return
		except Exception:
			from sys import exc_info
			print(f"[InfoBarGenerics] InfoBarSeek: Error in 'def doSeekRelative' {exc_info()[:2]}!")
		seekable = self.getSeek()
		if seekable is None or int(seekable.getLength()[1]) < 1:
			return
		prevstate = self.seekstate
		if self.seekstate == self.SEEK_STATE_EOF:
			if prevstate == self.SEEK_STATE_PAUSE:
				self.setSeekState(self.SEEK_STATE_PAUSE)
			else:
				self.setSeekState(self.SEEK_STATE_PLAY)
		seekable.seekRelative(pts < 0 and -1 or 1, abs(pts))
		if (abs(pts) > 100 or not config.usage.show_infobar_locked_on_pause.value) and config.usage.show_infobar_on_skip.value:
			self.showAfterSeek()

	def DoSeekAction(self):
		if self.seekAction > int(config.seek.withjumps_after_ff_speed.getValue()):
			self.doSeekRelativeAvoidStall(self.seekAction * int(config.seek.withjumps_forwards_ms.getValue()) * 90)
		elif self.seekAction < 0:
			self.doSeekRelativeAvoidStall(self.seekAction * int(config.seek.withjumps_backwards_ms.getValue()) * 90)
		for c in self.onPlayStateChanged:
			if self.seekAction > int(config.seek.withjumps_after_ff_speed.getValue()):  # Forward.
				c((0, self.seekAction, 0, ">> %dx" % self.seekAction))
			elif self.seekAction < 0:  # Backward.
				c((0, self.seekAction, 0, "<< %dx" % abs(self.seekAction)))
		if self.seekAction == 0:
			self.LastseekAction = False
			self.doPause(False)
			# global seek_withjumps_muted
			# seek_withjumps_muted = False
			self.setSeekState(self.SEEK_STATE_PLAY)

	def isServiceTypeTS(self):
		ref = self.session.nav.getCurrentlyPlayingServiceReference()
		isTS = False
		if ref is not None:
			servincetype = ServiceReference(ref).getType()
			if servincetype == 1:
				isTS = True
		return isTS

	def seekFwd(self):
		if config.seek.withjumps.value and not self.isServiceTypeTS():
			self.seekFwd_new()
		else:
			self.seekFwd_old()

	def seekBack(self):
		if config.seek.withjumps.value and not self.isServiceTypeTS():
			self.seekBack_new()
		else:
			self.seekBack_old()

	def seekFwd_new(self):
		self.LastseekAction = True
		self.doPause(True)
		# global seek_withjumps_muted
		# seek_withjumps_muted = True
		if self.seekAction >= 0:
			self.seekAction = self.getHigher(abs(self.seekAction), config.seek.speeds_forward.value) or config.seek.speeds_forward.value[-1]
		else:
			self.seekAction = -self.getLower(abs(self.seekAction), config.seek.speeds_backward.value)
		if (self.seekAction > 1) and (self.seekAction <= int(config.seek.withjumps_after_ff_speed.getValue())):  # Use fast forward for the configured speeds.
			self.setSeekState(self.makeStateForward(self.seekAction))
		elif self.seekAction > int(config.seek.withjumps_after_ff_speed.getValue()):  # We first need to go the play state, to stop fast forward.
			self.setSeekState(self.SEEK_STATE_PLAY)

	def seekBack_new(self):
		self.LastseekAction = True
		self.doPause(True)
		# global seek_withjumps_muted
		# seek_withjumps_muted = True
		if self.seekAction <= 0:
			self.seekAction = -self.getHigher(abs(self.seekAction), config.seek.speeds_backward.value) or -config.seek.speeds_backward.value[-1]
		else:
			self.seekAction = self.getLower(abs(self.seekAction), config.seek.speeds_forward.value)
		if (self.seekAction > 1) and (self.seekAction <= int(config.seek.withjumps_after_ff_speed.getValue())):  # Use fast forward for the configured forwards speeds.
			self.setSeekState(self.makeStateForward(self.seekAction))

	def seekFwd_old(self):
		seek = self.getSeek()
		if seek and not (seek.isCurrentlySeekable() & 2):
			if not self.fast_winding_hint_message_showed and (seek.isCurrentlySeekable() & 1):
				self.session.open(MessageBox, _("No fast winding possible yet.. but you can use the number buttons to skip forward/backward!"), MessageBox.TYPE_INFO, timeout=10)
				self.fast_winding_hint_message_showed = True
				return
			return 0  # Treat as unhandled action.
		if self.seekstate == self.SEEK_STATE_PLAY:
			self.setSeekState(self.makeStateForward(int(config.seek.enter_forward.value)))
		elif self.seekstate == self.SEEK_STATE_PAUSE:
			if len(config.seek.speeds_slowmotion.value):
				self.setSeekState(self.makeStateSlowMotion(config.seek.speeds_slowmotion.value[-1]))
			else:
				self.setSeekState(self.makeStateForward(int(config.seek.enter_forward.value)))
		elif self.seekstate == self.SEEK_STATE_EOF:
			pass
		elif self.isStateForward(self.seekstate):
			speed = self.seekstate[1]
			if self.seekstate[2]:
				speed /= self.seekstate[2]
			speed = self.getHigher(speed, config.seek.speeds_forward.value) or config.seek.speeds_forward.value[-1]
			self.setSeekState(self.makeStateForward(speed))
		elif self.isStateBackward(self.seekstate):
			speed = -self.seekstate[1]
			if self.seekstate[2]:
				speed /= self.seekstate[2]
			speed = self.getLower(speed, config.seek.speeds_backward.value)
			if speed:
				self.setSeekState(self.makeStateBackward(speed))
			else:
				self.setSeekState(self.SEEK_STATE_PLAY)
		elif self.isStateSlowMotion(self.seekstate):
			speed = self.getLower(self.seekstate[2], config.seek.speeds_slowmotion.value) or config.seek.speeds_slowmotion.value[0]
			self.setSeekState(self.makeStateSlowMotion(speed))

	def seekBack_old(self):
		seek = self.getSeek()
		if seek and not (seek.isCurrentlySeekable() & 2):
			if not self.fast_winding_hint_message_showed and (seek.isCurrentlySeekable() & 1):
				self.session.open(MessageBox, _("No fast winding possible yet.. but you can use the number buttons to skip forward/backward!"), MessageBox.TYPE_INFO, timeout=10)
				self.fast_winding_hint_message_showed = True
				return
			return 0  # Treat as unhandled action.
		seekstate = self.seekstate
		if seekstate == self.SEEK_STATE_PLAY:
			self.setSeekState(self.makeStateBackward(int(config.seek.enter_backward.value)))
		elif seekstate == self.SEEK_STATE_EOF:
			self.setSeekState(self.makeStateBackward(int(config.seek.enter_backward.value)))
			self.doSeekRelative(-6)
		elif seekstate == self.SEEK_STATE_PAUSE:
			self.doSeekRelative(-1)
		elif self.isStateForward(seekstate):
			speed = seekstate[1]
			if seekstate[2]:
				speed /= seekstate[2]
			speed = self.getLower(speed, config.seek.speeds_forward.value)
			if speed:
				self.setSeekState(self.makeStateForward(speed))
			else:
				self.setSeekState(self.SEEK_STATE_PLAY)
		elif self.isStateBackward(seekstate):
			speed = -seekstate[1]
			if seekstate[2]:
				speed /= seekstate[2]
			speed = self.getHigher(speed, config.seek.speeds_backward.value) or config.seek.speeds_backward.value[-1]
			self.setSeekState(self.makeStateBackward(speed))
		elif self.isStateSlowMotion(seekstate):
			speed = self.getHigher(seekstate[2], config.seek.speeds_slowmotion.value)
			if speed:
				self.setSeekState(self.makeStateSlowMotion(speed))
			else:
				self.setSeekState(self.SEEK_STATE_PAUSE)
		self.pts_lastseekspeed = self.seekstate[1]

	def seekFwdManual(self, fwd=True):
		if config.seek.baractivation.value == "leftright":
			self.session.open(SeekBar)
		else:
			self.session.openWithCallback(self.fwdSeekTo, MinuteInput)

	def seekBackManual(self, fwd=False):
		if config.seek.baractivation.value == "leftright":
			self.session.open(SeekBar)
		else:
			self.session.openWithCallback(self.rwdSeekTo, MinuteInput)

	def seekFwdVod(self, fwd=True):
		seekable = self.getSeek()
		if seekable is None:
			return
		else:
			if config.seek.baractivation.value == "leftright":
				self.session.open(SeekBar)
			else:
				self.session.openWithCallback(self.fwdSeekTo, MinuteInput)

	def seekFwdSeekbar(self, fwd=True):
		if not config.seek.baractivation.value == "leftright":
			self.session.open(SeekBar)
		else:
			self.session.openWithCallback(self.fwdSeekTo, MinuteInput)

	def fwdSeekTo(self, minutes):
		self.doSeekRelative(minutes * 60 * 90000)

	def seekBackSeekbar(self, fwd=False):
		if not config.seek.baractivation.value == "leftright":
			self.session.open(SeekBar)
		else:
			self.session.openWithCallback(self.rwdSeekTo, MinuteInput)

	def rwdSeekTo(self, minutes):
		# print("rwdSeekTo")
		self.doSeekRelative(-minutes * 60 * 90000)

	def checkSkipShowHideLock(self):
		if self.seekstate == self.SEEK_STATE_PLAY or self.seekstate == self.SEEK_STATE_EOF:
			self.lockedBecauseOfSkipping = False
			self.unlockShow()
		elif self.seekstate == self.SEEK_STATE_PAUSE and not config.usage.show_infobar_locked_on_pause.value:
			if config.usage.show_infobar_on_skip.value:
				self.lockedBecauseOfSkipping = False
				self.unlockShow()
				self.showAfterSeek()
		else:
			wantlock = self.seekstate != self.SEEK_STATE_PLAY
			if config.usage.show_infobar_on_skip.value:
				if self.lockedBecauseOfSkipping and not wantlock:
					self.unlockShow()
					self.lockedBecauseOfSkipping = False

				if wantlock and not self.lockedBecauseOfSkipping:
					self.lockShow()
					self.lockedBecauseOfSkipping = True

	def calcRemainingTime(self):
		seekable = self.getSeek()
		if seekable is not None:
			len = seekable.getLength()
			try:
				tmp = self.cueGetEndCutPosition()
				if tmp:
					len = (False, tmp)
			except Exception:
				pass
			pos = seekable.getPlayPosition()
			speednom = self.seekstate[1] or 1
			speedden = self.seekstate[2] or 1
			if not len[0] and not pos[0]:
				if len[1] <= pos[1]:
					return 0
				time = (len[1] - pos[1]) * speedden // (90 * speednom)
				return time
		return False

	def __evEOF(self):
		if self.seekstate == self.SEEK_STATE_EOF:
			return
		# global seek_withjumps_muted
		# if seek_withjumps_muted and eDVBVolumecontrol.getInstance().isMuted():
		# 	print("[InfoBarGenerics] STILL MUTED AFTER FFWD/FBACK !!!!!!!! so we unMute")
		# 	seek_withjumps_muted = False
		# 	eDVBVolumecontrol.getInstance().volumeUnMute()
		# If we are seeking forward, we try to end up ~1s before the end, and pause there.
		seekstate = self.seekstate
		if self.seekstate != self.SEEK_STATE_PAUSE:
			self.setSeekState(self.SEEK_STATE_EOF)
		if seekstate not in (self.SEEK_STATE_PLAY, self.SEEK_STATE_PAUSE):  # If we are seeking.
			seekable = self.getSeek()
			if seekable is not None:
				seekable.seekTo(-1)
				self.doEofInternal(True)
		if seekstate == self.SEEK_STATE_PLAY:  # Regular EOF.
			self.doEofInternal(True)
		else:
			self.doEofInternal(False)

	def doEofInternal(self, playing):
		pass  # Defined in subclasses.

	def __evSOF(self):
		self.setSeekState(self.SEEK_STATE_PLAY)
		self.doSeek(0)


class InfoBarPVRState:
	def __init__(self, screen=PVRState, force_show=False):
		self.onChangedEntry = []
		self.onPlayStateChanged.append(self.__playStateChanged)
		self.pvrStateDialog = self.session.instantiateDialog(screen)
		self.pvrStateDialog.setAnimationMode(0)
		self.onShow.append(self._mayShow)
		self.onHide.append(self.pvrStateDialog.hide)
		self.force_show = force_show

	def createSummary(self):
		return InfoBarMoviePlayerSummary

	def _mayShow(self):
		if "state" in self and not config.usage.movieplayer_pvrstate.value:
			self["state"].setText("")
			self["statusicon"].setPixmapNum(6)
			self["speed"].setText("")
		if config.usage.show_infobar_do_dimming.value is True:
			if self.shown and self.seekstate != self.SEEK_STATE_EOF and not config.usage.movieplayer_pvrstate.value:
				self.DimmingTimer.stop()
				self.doWriteAlpha(config.av.osd_alpha.value)
				self.pvrStateDialog.show()
				self.startHideTimer()

	def __playStateChanged(self, state):
		playstateString = state[3]
		state_summary = playstateString
		if "statusicon" in self.pvrStateDialog:
			self.pvrStateDialog["state"].setText(playstateString)
			if playstateString == ">":
				self.pvrStateDialog["statusicon"].setPixmapNum(0)
				self.pvrStateDialog["speed"].setText("")
				speed_summary = self.pvrStateDialog["speed"].text
				statusicon_summary = 0
				if "state" in self and config.usage.movieplayer_pvrstate.value:
					self["state"].setText(playstateString)
					self["statusicon"].setPixmapNum(0)
					self["speed"].setText("")
			elif playstateString == "||":
				self.pvrStateDialog["statusicon"].setPixmapNum(1)
				self.pvrStateDialog["speed"].setText("")
				speed_summary = self.pvrStateDialog["speed"].text
				statusicon_summary = 1
				if "state" in self and config.usage.movieplayer_pvrstate.value:
					self["state"].setText(playstateString)
					self["statusicon"].setPixmapNum(1)
					self["speed"].setText("")
			elif playstateString == "END":
				self.pvrStateDialog["statusicon"].setPixmapNum(2)
				self.pvrStateDialog["speed"].setText("")
				speed_summary = self.pvrStateDialog["speed"].text
				statusicon_summary = 2
				if "state" in self and config.usage.movieplayer_pvrstate.value:
					self["state"].setText(playstateString)
					self["statusicon"].setPixmapNum(2)
					self["speed"].setText("")
			elif playstateString.startswith(">>"):
				speed = state[3].split()
				self.pvrStateDialog["statusicon"].setPixmapNum(3)
				self.pvrStateDialog["speed"].setText(speed[1])
				speed_summary = self.pvrStateDialog["speed"].text
				statusicon_summary = 3
				if "state" in self and config.usage.movieplayer_pvrstate.value:
					self["state"].setText(playstateString)
					self["statusicon"].setPixmapNum(3)
					self["speed"].setText(speed[1])
			elif playstateString.startswith("<<"):
				speed = state[3].split()
				self.pvrStateDialog["statusicon"].setPixmapNum(4)
				self.pvrStateDialog["speed"].setText(speed[1])
				speed_summary = self.pvrStateDialog["speed"].text
				statusicon_summary = 4
				if "state" in self and config.usage.movieplayer_pvrstate.value:
					self["state"].setText(playstateString)
					self["statusicon"].setPixmapNum(4)
					self["speed"].setText(speed[1])
			elif playstateString.startswith("/"):
				self.pvrStateDialog["statusicon"].setPixmapNum(5)
				self.pvrStateDialog["speed"].setText(playstateString)
				speed_summary = self.pvrStateDialog["speed"].text
				statusicon_summary = 5
				if "state" in self and config.usage.movieplayer_pvrstate.value:
					self["state"].setText(playstateString)
					self["statusicon"].setPixmapNum(5)
					self["speed"].setText(playstateString)
			for cb in self.onChangedEntry:
				cb(state_summary, speed_summary, statusicon_summary)
		# If we return into "PLAY" state, ensure that the dialog gets hidden if there will be no InfoBar displayed.
		if not config.usage.show_infobar_on_skip.value and self.seekstate == self.SEEK_STATE_PLAY and not self.force_show:
			self.pvrStateDialog.hide()
		else:
			self._mayShow()


class TimeshiftLive(Screen):
	def __init__(self, session):
		Screen.__init__(self, session)


class InfoBarTimeshiftState(InfoBarPVRState):
	def __init__(self):
		InfoBarPVRState.__init__(self, screen=TimeshiftState, force_show=True)
		self.timeshiftLiveScreen = self.session.instantiateDialog(TimeshiftLive)
		self.onHide.append(self.timeshiftLiveScreen.hide)
		if isStandardInfoBar(self):
			self.secondInfoBarScreen and self.secondInfoBarScreen.onShow.append(self.timeshiftLiveScreen.hide)
			self.secondInfoBarScreenSimple and self.secondInfoBarScreenSimple.onShow.append(self.timeshiftLiveScreen.hide)
		self.timeshiftLiveScreen.hide()
		self.__hideTimer = eTimer()
		self.__hideTimer.callback.append(self.__hideTimeshiftState)
		self.onFirstExecBegin.append(self.pvrStateDialog.show)

	def _mayShow(self):
		if self.timeshiftEnabled():
			if isStandardInfoBar(self):
				if self.secondInfoBarScreen and self.secondInfoBarScreen.shown:
					self.secondInfoBarScreen.hide()
				if self.secondInfoBarScreenSimple and self.secondInfoBarScreenSimple.shown:
					self.secondInfoBarScreenSimple.hide()
			if self.timeshiftActivated():
				self.pvrStateDialog.show()
				self.timeshiftLiveScreen.hide()
			elif self.showTimeshiftState:
				self.pvrStateDialog.hide()
				self.timeshiftLiveScreen.show()
				self.showTimeshiftState = False
			if self.seekstate == self.SEEK_STATE_PLAY and config.usage.infobar_timeout.index and (self.pvrStateDialog.shown or self.timeshiftLiveScreen.shown):
				self.__hideTimer.startLongTimer(config.usage.infobar_timeout.index)
		else:
			self.__hideTimeshiftState()

	def __hideTimeshiftState(self):
		self.pvrStateDialog.hide()
		self.timeshiftLiveScreen.hide()


class InfoBarShowMovies:

	# I don't really like this class.
	# It calls a not further specified "movie list" on up/down/movieList,
	# so this is not more than an action map.
	def __init__(self):
		self["MovieListActions"] = HelpableActionMap(self, ["InfobarMovieListActions"],
			{
				"movieList": (self.showMovies, _("Open the movie list")),
				"up": (self.up, _("Open the movie list")),
				"down": (self.down, _("Open the movie list"))
			})

# InfoBarTimeshift requires InfoBarSeek, instantiated BEFORE!

# Hrmf.
#
# Timeshift works the following way:
# demux0   demux1					 "TimeshiftActions" "TimeshiftActivateActions" "SeekActions"
# - normal playback						  TUNER	   unused	   PLAY				  enable				disable				 disable
# - user presses "yellow" button.		  FILE	   record	   PAUSE			  enable				disable				 enable
# - user presess pause again			  FILE	   record	   PLAY				  enable				disable				 enable
# - user fast forwards					  FILE	   record	   FF				  enable				disable				 enable
# - end of timeshift buffer reached		  TUNER	   record	   PLAY				  enable				enable				 disable
# - user backwards						  FILE	   record	   BACK  # !!		  enable				disable				 enable
#

# in other words:
# - when a service is playing, pressing the "timeshiftStart" button ("yellow") enables recording ("enables timeshift"),
# freezes the picture (to indicate timeshift), sets timeshiftMode ("activates timeshift")
# now, the service becomes seekable, so "SeekActions" are enabled, "TimeshiftEnableActions" are disabled.
# - the user can now PVR around
# - if it hits the end, the service goes into live mode ("deactivates timeshift", it's of course still "enabled")
# the service looses it's "seekable" state. It can still be paused, but just to activate timeshift right
# after!
# the seek actions will be disabled, but the timeshiftActivateActions will be enabled
# - if the user rewinds, or press pause, timeshift will be activated again

# note that a timeshift can be enabled ("recording") and
# activated (currently time-shifting).


class InfoBarTimeshift:
	def __init__(self):
		self["TimeshiftActions"] = HelpableActionMap(self, ["InfobarTimeshiftActions"],
			{
				"timeshiftStart": (self.startTimeshift, _("Start timeshift")),  # the "yellow key"
				"timeshiftStop": (self.stopTimeshift, _("Stop timeshift")),      # currently undefined :), probably 'TV'
				"seekFwdManual": (self.seekFwdManual, _("Seek forward (enter time)")),
				"seekBackManual": (self.seekBackManual, _("Seek backward (enter time)")),
				"seekdef:1": (boundFunction(self.seekdef, 1), _("Seek")),
				"seekdef:3": (boundFunction(self.seekdef, 3), _("Seek")),
				"seekdef:4": (boundFunction(self.seekdef, 4), _("Seek")),
				"seekdef:6": (boundFunction(self.seekdef, 6), _("Seek")),
				"seekdef:7": (boundFunction(self.seekdef, 7), _("Seek")),
				"seekdef:9": (boundFunction(self.seekdef, 9), _("Seek")),
			}, prio=0)
		self["TimeshiftActivateActions"] = ActionMap(["InfobarTimeshiftActivateActions"],
			{
				"timeshiftActivateEnd": self.activateTimeshiftEnd,  # something like "rewind key"
				"timeshiftActivateEndAndPause": self.activateTimeshiftEndAndPause  # something like "pause key"
			}, prio=-1)  # priority over record

		self["TimeshiftActivateActions"].setEnabled(False)
		self.ts_rewind_timer = eTimer()
		self.ts_rewind_timer.callback.append(self.rewindService)
		self.ts_start_delay_timer = eTimer()
		self.ts_start_delay_timer.callback.append(self.startTimeshiftWithoutPause)
		self.ts_current_event_timer = eTimer()
		self.ts_current_event_timer.callback.append(self.saveTimeshiftFileForEvent)
		self.save_timeshift_file = False
		self.timeshift_was_activated = False
		self.showTimeshiftState = False
		self.save_timeshift_only_current_event = False

		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evStart: self.__serviceStarted,
				iPlayableService.evSeekableStatusChanged: self.__seekableStatusChanged,
				iPlayableService.evEnd: self.__serviceEnd
			})

	def seekdef(self, key):
		if self.seekstate == self.SEEK_STATE_PLAY:
			return 0  # trade as unhandled action
		time = (-config.seek.selfdefined_13.value, False, config.seek.selfdefined_13.value,
			-config.seek.selfdefined_46.value, False, config.seek.selfdefined_46.value,
			-config.seek.selfdefined_79.value, False, config.seek.selfdefined_79.value)[key - 1]
		self.doSeekRelative(time * 90000)
		self.pvrStateDialog.show()
		return 1

	def getTimeshift(self):
		service = self.session.nav.getCurrentService()
		return service and service.timeshift()

	def timeshiftEnabled(self):
		ts = self.getTimeshift()
		return ts and ts.isTimeshiftEnabled()

	def timeshiftActivated(self):
		ts = self.getTimeshift()
		return ts and ts.isTimeshiftActive()

	def playpauseStreamService(self):
		service = self.session.nav.getCurrentService()
		playingref = self.session.nav.getCurrentlyPlayingServiceReference()
		if not playingref or playingref.type < eServiceReference.idUser:
			return 0
		if service and service.streamed():
			pauseable = service.pause()
			if pauseable:
				if self.seekstate == self.SEEK_STATE_PLAY:
					pauseable.pause()
					self.pvrStateDialog.show()
					self.seekstate = self.SEEK_STATE_PAUSE
				else:
					pauseable.unpause()
					self.pvrStateDialog.hide()
					self.seekstate = self.SEEK_STATE_PLAY
				return
		return 0

	def startTimeshift(self, pauseService=True):
		print("[InfoBarGenerics] enable timeshift")
		ts = self.getTimeshift()
		if ts is None:
			if not pauseService and not int(config.usage.timeshift_start_delay.value):
				self.session.open(MessageBox, _("Timeshift not possible!"), MessageBox.TYPE_ERROR, simple=True)
			print("[InfoBarGenerics] no ts interface")
			if pauseService:
				return self.playpauseStreamService()
			else:
				return 0

		if ts.isTimeshiftEnabled():
			print("[InfoBarGenerics] timeshift already enabled?")
		else:
			if not ts.startTimeshift():
				# we remove the "relative time" for now.
				# self.pvrStateDialog["timeshift"].setRelative(time.time())

				if pauseService:
					# PAUSE.
					# self.setSeekState(self.SEEK_STATE_PAUSE)
					self.activateTimeshiftEnd(False)
					self.showTimeshiftState = True
				else:
					self.showTimeshiftState = False

				# enable the "TimeshiftEnableActions", which will override
				# the startTimeshift actions
				self.__seekableStatusChanged()

				# get current timeshift filename and calculate new
				self.save_timeshift_file = False
				self.save_timeshift_in_movie_dir = False
				self.setCurrentEventTimer()
				self.current_timeshift_filename = ts.getTimeshiftFilename()
				self.new_timeshift_filename = self.generateNewTimeshiftFileName()
				self.setLCDsymbolTimeshift()
			else:
				print("[InfoBarGenerics] timeshift failed")

	def startTimeshiftWithoutPause(self):
		self.startTimeshift(False)

	def stopTimeshift(self):
		ts = self.getTimeshift()
		if ts and ts.isTimeshiftEnabled():
			if int(config.usage.timeshift_start_delay.value):
				ts.switchToLive()
			else:
				self.checkTimeshiftRunning(self.stopTimeshiftcheckTimeshiftRunningCallback)
		else:
			return 0

	def stopTimeshiftcheckTimeshiftRunningCallback(self, answer):
		ts = self.getTimeshift()
		if answer and ts:
			ts.stopTimeshift()
			self.pvrStateDialog.hide()
			self.setCurrentEventTimer()
			self.setLCDsymbolTimeshift()
			# disable actions
			self.__seekableStatusChanged()

	# activates timeshift, and seeks to (almost) the end
	def activateTimeshiftEnd(self, back=True):
		self.showTimeshiftState = True
		ts = self.getTimeshift()
		print("[InfoBarGenerics] activateTimeshiftEnd")

		if ts is None:
			return

		if ts.isTimeshiftActive():
			print("[InfoBarGenerics] activate timeshift called - but shouldn't this be a normal pause?")
			self.pauseService()
		else:
			print("[InfoBarGenerics] play, ...")
			ts.activateTimeshift()  # activate timeshift will automatically pause
			self.setSeekState(self.SEEK_STATE_PAUSE)
			seekable = self.getSeek()
			if seekable is not None:
				seekable.seekTo(-90000)  # seek approx. 1 sec before end
			self.timeshift_was_activated = True
		if back:
			self.ts_rewind_timer.start(200, 1)

	def rewindService(self):
		self.setSeekState(self.makeStateBackward(int(config.seek.enter_backward.value)))

	# generates only filename without path
	def generateNewTimeshiftFileName(self):
		name = "timeshift record"
		info = {}
		self.getProgramInfoAndEvent(info, name)

		serviceref = info["serviceref"]

		service_name = ""
		if isinstance(serviceref, eServiceReference):
			service_name = ServiceReference(serviceref).getServiceName()
		begin_date = strftime("%Y%m%d %H%M", localtime(time()))
		filename = begin_date + " - " + service_name

		if config.recording.filename_composition.value == "event":
			filename = "%s - %s_%s" % (info["name"], strftime("%Y%m%d %H%M", localtime(time())), service_name)
		elif config.recording.filename_composition.value == "short":
			filename = strftime("%Y%m%d", localtime(time())) + " - " + info["name"]
		elif config.recording.filename_composition.value == "long":
			filename += " - " + info["name"] + " - " + info["description"]
		else:
			filename += " - " + info["name"]  # standard

		if config.recording.ascii_filenames.value:
			filename = legacyEncode(filename)

		print("[InfoBarGenerics] New timeshift filename: ", filename)
		return filename

	# same as activateTimeshiftEnd, but pauses afterwards.
	def activateTimeshiftEndAndPause(self):
		print("[InfoBarGenerics] activateTimeshiftEndAndPause")
		# state = self.seekstate
		self.activateTimeshiftEnd(False)

	def callServiceStarted(self):
		self.__serviceStarted()

	def __seekableStatusChanged(self):
		self["TimeshiftActivateActions"].setEnabled(not self.isSeekable() and self.timeshiftEnabled())
		state = self.getSeek() is not None and self.timeshiftEnabled()
		self["SeekActions"].setEnabled(state)
		if not state:
			self.setSeekState(self.SEEK_STATE_PLAY)
		self.restartSubtitle()

	def setLCDsymbolTimeshift(self):
		if BoxInfo.getItem("LCDsymbol_timeshift"):
			open(BoxInfo.getItem("LCDsymbol_timeshift"), "w").write(self.timeshiftEnabled() and "1" or "0")

	def __serviceStarted(self):
		self.pvrStateDialog.hide()
		self.__seekableStatusChanged()
		if self.ts_start_delay_timer.isActive():
			self.ts_start_delay_timer.stop()
		if int(config.usage.timeshift_start_delay.value):
			self.ts_start_delay_timer.start(int(config.usage.timeshift_start_delay.value) * 1000, True)

	def checkTimeshiftRunning(self, returnFunction, timeout=-1):
		if self.timeshiftEnabled() and config.usage.check_timeshift.value and self.timeshift_was_activated:
			message = _("Stop timeshift?")
			if not self.save_timeshift_file:
				choice = [(_("Yes"), "stop"), (_("No"), "continue"), (_("Yes and save"), "save"), (_("Yes and save in movie dir"), "save_movie")]
			else:
				choice = [(_("Yes"), "stop"), (_("No"), "continue")]
				message += "\n" + _("Reminder, you have chosen to save timeshift file.")
				if self.save_timeshift_only_current_event:
					remaining = self.currentEventTime()
					if remaining > 0:
						message += "\n" + _("The %d min remaining before the end of the event.") % abs(remaining / 60)
			self.session.openWithCallback(boundFunction(self.checkTimeshiftRunningCallback, returnFunction), MessageBox, message, timeout=timeout, simple=True, list=choice)
		else:
			returnFunction(True)

	def checkTimeshiftRunningCallback(self, returnFunction, answer):
		if answer:
			if "movie" in answer:
				self.save_timeshift_in_movie_dir = True
			if "save" in answer:
				self.save_timeshift_file = True
				ts = self.getTimeshift()
				if ts:
					ts.saveTimeshiftFile()
					del ts
			if "continue" not in answer:
				self.saveTimeshiftFiles()
		returnFunction(answer and answer != "continue")

	# renames/moves timeshift files if requested
	def __serviceEnd(self):
		self.saveTimeshiftFiles()
		self.setCurrentEventTimer()
		self.setLCDsymbolTimeshift()
		self.timeshift_was_activated = False

	def saveTimeshiftFiles(self):
		if self.save_timeshift_file and self.current_timeshift_filename and self.new_timeshift_filename:
			if config.usage.timeshift_path.value and not self.save_timeshift_in_movie_dir:
				dirname = config.usage.timeshift_path.value
			else:
				dirname = defaultMoviePath()
			filename = getRecordingFilename(self.new_timeshift_filename, dirname) + ".ts"

			fileList = []
			fileList.append((self.current_timeshift_filename, filename))
			if fileExists(self.current_timeshift_filename + ".sc"):
				fileList.append((self.current_timeshift_filename + ".sc", filename + ".sc"))
			if fileExists(self.current_timeshift_filename + ".cuts"):
				fileList.append((self.current_timeshift_filename + ".cuts", filename + ".cuts"))

			moveFiles(fileList)
			self.save_timeshift_file = False
			self.setCurrentEventTimer()

	def currentEventTime(self):
		remaining = 0
		ref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if ref:
			epg = eEPGCache.getInstance()
			event = epg.lookupEventTime(ref, -1, 0)
			if event:
				now = int(time())
				start = event.getBeginTime()
				duration = event.getDuration()
				end = start + duration
				remaining = end - now
		return remaining

	def saveTimeshiftFileForEvent(self):
		if self.timeshiftEnabled() and self.save_timeshift_only_current_event and self.timeshift_was_activated and self.save_timeshift_file:
			message = _("Current event is over.\nSelect an option to save the timeshift file.")
			choice = [(_("Save and stop timeshift"), "save"), (_("Save and restart timeshift"), "restart"), (_("Don't save and stop timeshift"), "stop"), (_("Do nothing"), "continue")]
			self.session.openWithCallback(self.saveTimeshiftFileForEventCallback, MessageBox, message, simple=True, list=choice, timeout=15)

	def saveTimeshiftFileForEventCallback(self, answer):
		self.save_timeshift_only_current_event = False
		if answer:
			ts = self.getTimeshift()
			if ts and answer in ("save", "restart", "stop"):
				self.stopTimeshiftcheckTimeshiftRunningCallback(True)
				if answer in ("save", "restart"):
					ts.saveTimeshiftFile()
					del ts
					self.saveTimeshiftFiles()
				if answer == "restart":
					self.ts_start_delay_timer.start(1000, True)
				self.save_timeshift_file = False
				self.save_timeshift_in_movie_dir = False

	def setCurrentEventTimer(self, duration=0):
		self.ts_current_event_timer.stop()
		self.save_timeshift_only_current_event = False
		if duration > 0:
			self.save_timeshift_only_current_event = True
			self.ts_current_event_timer.startLongTimer(duration)


class ExtensionsList(ChoiceBox):
	def __init__(self, session, extensions):
		colorKeys = {
			"red": 1,
			"green": 2,
			"yellow": 3,
			"blue": 4
		}
		extensionListAll = []
		for extension in extensions:
			if extension[0] == InfoBarExtensions.EXTENSION_SINGLE:
				if extension[1][2]():
					extensionListAll.append((extension[1][0](), extension[1], extension[2], colorKeys.get(extension[2], 0)))
			else:
				for subExtension in extension[1]():
					if subExtension[0][2]():
						extensionListAll.append((subExtension[0][0](), subExtension[0], subExtension[1], colorKeys.get(subExtension[1], 0)))
		if config.usage.sortExtensionslist.value == "alpha":
			extensionListAll.sort(key=lambda x: (x[3], x[0]))
		else:
			extensionListAll.sort(key=lambda x: x[3])
		allKeys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]
		extensionList = []
		extensionKeys = []
		for extension in extensionListAll:
			key = extension[2]
			if not key and allKeys:
				key = allKeys.pop(0)
			extensionKeys.append(key or "")
			extensionList.append((extension[0], extension[1]))

		reorderConfig = "extensionOrder" if config.usage.sortExtensionslist.value == "user" else ""
		ChoiceBox.__init__(self, session, title=_("Extensions"), list=extensionList, keys=extensionKeys, reorderConfig=reorderConfig, skin_name="ExtensionsList")


class InfoBarExtensions:
	EXTENSION_SINGLE = 0
	EXTENSION_LIST = 1

	def __init__(self):
		self.list = []
		self.addExtension((lambda: _("Softcam Setup"), self.openSoftcamSetup, lambda: config.misc.softcam_setup.extension_menu.value and BoxInfo.getItem("HasSoftcamInstalled")), "1")
		self.addExtension((lambda: _("Manually import from fallback tuner"), self.importChannels, lambda: config.usage.remote_fallback_extension_menu.value and config.usage.remote_fallback_import.value))
		self["InstantExtensionsActions"] = HelpableActionMap(self, ["InfobarExtensions"], {
				"extensions": (self.showExtensionSelection, _("Show extensions...")),
		},prio=1, description=_("Extension Actions"))  # Lower priority.
		self.addExtension(extension=self.getOScamInfo, type=InfoBarExtensions.EXTENSION_LIST)
		self.addExtension(extension=self.getLogManager, type=InfoBarExtensions.EXTENSION_LIST)

	def getOSname(self):
		return _("OScam/Ncam Info")

	def getOScamInfo(self):
		import process
		p = process.ProcessList()
		oscam = str(p.named("oscam")).strip("[]") or str(p.named("oscam-emu")).strip("[]") or str(p.named("ncam")).strip("[]")
		if oscam:
			return [((boundFunction(self.getOSname), boundFunction(self.openOScamInfo), lambda: True), None)] or []
		else:
			return []

	def openSoftcamSetup(self):
		from Screens.SoftcamSetup import SoftcamSetup
		self.session.open(SoftcamSetup)

	def getLogManagerName(self):
		return _("Logs Manager")

	def getLogManager(self):
		if config.logmanager.showinextensions.value:
			return [((boundFunction(self.getLogManagerName), boundFunction(self.openLogManager), lambda: True), None)]
		else:
			return []

	def importChannels(self):
		from Components.ImportChannels import ImportChannels
		ImportChannels()

	def addExtension(self, extension, key=None, type=EXTENSION_SINGLE):
		self.list.append((type, extension, key))

	def updateExtension(self, extension, key=None):
		self.extensionsList.append(extension)
		if key is not None and key in self.extensionKeys:
			key = None

		if key is None:
			for x in self.availableKeys:
				if x not in self.extensionKeys:
					key = x
					break

		if key is not None:
			self.extensionKeys[key] = len(self.extensionsList) - 1

	def updateExtensions(self):
		self.extensionsList = []
		self.availableKeys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "red", "green", "yellow", "blue"]
		self.extensionKeys = {}
		for x in self.list:
			if x[0] == self.EXTENSION_SINGLE:
				if x[1][2]():
					self.updateExtension(x[1], x[2])
			else:
				for y in x[1]():
					self.updateExtension(y[0], y[1])

	def showExtensionSelection(self):
		self.session.openWithCallback(self.extensionCallback, ExtensionsList, self.list)

	def extensionCallback(self, answer):
		if answer is not None:
			answer[1][1]()

	def openOScamInfo(self):
		from Screens.OScamInfo import OSCamInfo
		self.session.open(OSCamInfo)

	def openLogManager(self):
		from Screens.LogManager import LogManager
		self.session.open(LogManager)


class InfoBarPlugins:  # Depends on InfoBarExtensions.
	def __init__(self):
		self.addExtension(extension=self.getPluginList, type=InfoBarExtensions.EXTENSION_LIST)

	def getPluginList(self):  # Used in plugins
		pluginList = []
		for plugin in plugins.getPlugins(where=PluginDescriptor.WHERE_EXTENSIONSMENU):
			args = getfullargspec(plugin.__call__)[0] # FIME: This is a performance issue and should be replaced.
			if len(args) in (1, 2) and isinstance(self, InfoBarChannelSelection):
				pluginList.append(((boundFunction(self.getPluginName, plugin.name), boundFunction(self.runPlugin, plugin), lambda: True), None, plugin.name))
		pluginList.sort(key=lambda x: x[2])  # Sort by name.
		return pluginList

	def getPluginName(self, name):  # Used in plugins
		return name

	def runPlugin(self, plugin):  # Used in AudioSelection.py
		if isinstance(self, InfoBarChannelSelection):
			plugin(session=self.session, servicelist=self.servicelist)
		else:
			try:
				plugin(session=self.session)
			except Exception as err:
				print("[InfoBarGenerics] Error: ", err)
				print(f"[InfoBarGenerics] InfoBarPlugins: Error: {str(err)}!")


class InfoBarJobman:
	def __init__(self):
		self.addExtension(extension=self.getJobList, type=InfoBarExtensions.EXTENSION_LIST)

	def getJobList(self):
		return [((boundFunction(self.getJobName, job), boundFunction(self.showJobView, job), boundFunction(self.isActiveJob, job)), "refresh") for job in job_manager.getPendingJobs()]

	def getJobName(self, job):
		if job.status == job.IN_PROGRESS:
			return "%s: (%d%%), %s" % (job.getStatustext(), int(100 * job.progress / float(job.end)), job.name)
		return "%s: %s" % (job.getStatustext(), job.name)

	def showJobView(self, job):
		from Screens.TaskView import JobView
		job_manager.in_background = False
		self.session.openWithCallback(self.JobViewCB, JobView, job)

	def isActiveJob(self, job):
		return job.status in (job.IN_PROGRESS, job.NOT_STARTED)

	def JobViewCB(self, in_background):
		job_manager.in_background = in_background


# Depends on InfoBarExtensions
#
class InfoBarPiP:
	def __init__(self):
		try:
			self.session.pipshown
		except Exception:
			self.session.pipshown = False
		self.lastPiPService = None
		if BoxInfo.getItem("PIPAvailable") and isinstance(self, InfoBarEPG):
			self["PiPActions"] = HelpableActionMap(self, "InfobarPiPActions", {
				"activatePiP": (self.activePiP, self.activePiPName),
			}, prio=0, description=_("PiP Actions"))
			if self.allowPiP:
				self.addExtension((self.extShowHideName, self.showPiP, lambda: True), "blue")
				self.addExtension((self.extMoveName, self.movePiP, self.pipShown), "green")
				self.addExtension((self.extSwapName, self.swapPiP, lambda: self.pipShown() and isStandardInfoBar(self)), "yellow")
				self.addExtension((self.extTogglePipZapName, self.togglePipZap, self.pipShown), "red")
			else:
				self.addExtension((self.extShowHideName, self.showPiP, self.pipShown), "blue")
				self.addExtension((self.extMoveName, self.movePiP, self.pipShown), "green")
		self.lastPiPServiceTimeoutTimer = eTimer()
		self.lastPiPServiceTimeoutTimer.callback.append(self.clearLastPiPService)

	def pipShown(self):
		return self.session.pipshown

	def pipHandles0Action(self):
		return self.pipShown() and config.usage.pip_zero_button.value != "standard"

	def extShowHideName(self):
		return _("Disable Picture in Picture") if self.session.pipshown else _("Activate Picture in Picture")

	def extSwapName(self):
		return _("Swap services")

	def extMoveName(self):
		return _("Picture in Picture Setup")

	def getTogglePipzapName(self):
		slist = self.servicelist
		if slist and slist.dopipzap:
			return _("Zap focus to main screen")
		return _("Zap focus to Picture in Picture")

	def togglePipzap(self):  # called from ButtonSetup
		return self.togglePipZap()

	def togglePipZap(self):
		if not self.session.pipshown:
			self.showPiP()
		serviceList = self.servicelist
		if serviceList and self.session.pipshown:
			serviceList.togglePipzap()
			if serviceList.dopipzap:
				currentServicePath = serviceList.getCurrentServicePath()
				self.servicelist.setCurrentServicePath(self.session.pip.servicePath, doZap=False)
				self.session.pip.servicePath = currentServicePath

	def showPiP(self):
		self.lastPiPServiceTimeoutTimer.stop()
		slist = self.servicelist
		if self.session.pipshown:
			if slist and slist.dopipzap:
				self.togglePipzap()
			if self.session.pipshown:
				lastPiPServiceTimeout = int(config.usage.pip_last_service_timeout.value)
				if lastPiPServiceTimeout >= 0:
					self.lastPiPService = self.session.pip.getCurrentServiceReference()
					if lastPiPServiceTimeout:
						self.lastPiPServiceTimeoutTimer.startLongTimer(lastPiPServiceTimeout)
				del self.session.pip
				if BoxInfo.getItem("LCDMiniTV") and config.lcd.modepip.value >= 1:
					print("[InfoBarGenerics] [LCDMiniTV] disable PiP")
					eDBoxLCD.getInstance().setLCDMode(config.lcd.modeminitv.value)
				self.session.pipshown = False
			if hasattr(self, "screenSaverTimerStart"):
				self.screenSaverTimerStart()
		else:
			service = self.session.nav.getCurrentService()
			info = service and service.info()
			if info:
				xres = str(info.getInfo(iServiceInformation.sVideoWidth))
			if info and int(xres) <= 720 or BoxInfo.getItem("model") != "blackbox7405":
				self.session.pip = self.session.instantiateDialog(PictureInPicture)
				self.session.pip.setAnimationMode(0)
				self.session.pip.show()
				if isStandardInfoBar(self):
					newservice = self.lastPiPService or self.session.nav.getCurrentlyPlayingServiceReference() or self.servicelist.getCurrentSelection()
				else:
					newservice = self.lastPiPService or self.servicelist.getCurrentSelection()
				if self.session.pip.playService(newservice):
					self.session.pipshown = True
					self.session.pip.servicePath = self.servicelist.getCurrentServicePath()
					if BoxInfo.getItem("LCDMiniTVPiP") and config.lcd.modepip.value >= 1:
						print("[InfoBarGenerics] [LCDMiniTV] enable PiP")
						eDBoxLCD.getInstance().setLCDMode(config.lcd.modepip.value, True)
				else:
					newservice = self.session.nav.getCurrentlyPlayingServiceReference() or self.servicelist.servicelist.getCurrent()
					if self.session.pip.playService(newservice):
						self.session.pipshown = True
						self.session.pip.servicePath = self.servicelist.getCurrentServicePath()
						if BoxInfo.getItem("LCDMiniTVPiP") and config.lcd.modepip.value >= 1:
							print("[InfoBarGenerics] [LCDMiniTV] enable PiP")
							eDBoxLCD.getInstance().setLCDMode(config.lcd.modepip.value, True)
					else:
						self.lastPiPService = None
						self.session.pipshown = False
						del self.session.pip
			elif info:
				self.session.open(MessageBox, _("Your %s %s does not support PiP HD") % getBoxDisplayName(), type=MessageBox.TYPE_INFO, timeout=5)
			else:
				self.session.open(MessageBox, _("No active channel found."), type=MessageBox.TYPE_INFO, timeout=5)
		if self.session.pipshown and hasattr(self, "screenSaverTimer"):
			self.screenSaverTimer.stop()

	def clearLastPiPService(self):
		self.lastPiPService = None

	def activePiP(self):
		if self.servicelist and self.servicelist.dopipzap or not self.session.pipshown:
			self.showPiP()
		else:
			self.togglePipzap()

	def activePiPName(self):
		if self.servicelist and self.servicelist.dopipzap:
			return _("Disable Picture in Picture")
		if self.session.pipshown:
			return _("Zap focus to Picture in Picture")
		else:
			return _("Activate Picture in Picture")

	def swapPiP(self):
		if self.pipShown():
			swapService = self.session.nav.getCurrentlyPlayingServiceOrGroup()
			pipServiceReference = self.session.pip.getCurrentService()
			if swapService and pipServiceReference and pipServiceReference.toString() != swapService.toString():
				currentServicePath = self.servicelist.getCurrentServicePath()
				currentBouquet = self.servicelist and self.servicelist.getRoot()
				self.servicelist.setCurrentServicePath(self.session.pip.servicePath, doZap=False)
				self.session.pip.playService(swapService)
				self.session.nav.stopService()  # Stop portal.
				self.session.nav.playService(pipServiceReference, checkParentalControl=False, adjust=False)
				self.session.pip.servicePath = currentServicePath
				self.session.pip.servicePath[1] = currentBouquet
				if self.servicelist.dopipzap:  # This unfortunately won't work with subservices.
					self.servicelist.setCurrentSelection(self.session.pip.getCurrentService())

	def movePiP(self):
		if self.pipShown():
			self.session.open(PiPSetup, pip=self.session.pip)

	def extTogglePipZapName(self):
		use = config.usage.pip_zero_button.value
		if "swap" == use:
			self.swapPiP()
		elif "swapstop" == use:
			self.swapPiP()
			self.showPiP()
		elif "stop" == use:
			self.showPiP()


class InfoBarInstantRecord:
	"""Instant Record - Handles the instantRecord action in order to start/stop instant recordings."""

	def __init__(self):
		self["InstantRecordActions"] = HelpableActionMap(self, ["InfobarInstantRecord"],
			{
				"instantRecord": (self.instantRecord, _("Instant recording")),
			})
		self.SelectedInstantServiceRef = None
		if isStandardInfoBar(self):
			self.recording = []
		else:
			from Screens.InfoBar import InfoBar
			InfoBarInstance = InfoBar.instance
			if InfoBarInstance:
				self.recording = InfoBarInstance.recording

	def moveToTrash(self, entry):
		print("[instantRecord] stop and delete recording: %s", entry.name)
		from Tools.Trashcan import createTrashFolder
		trash = createTrashFolder(entry.Filename)
		from Screens.MovieSelection import moveServiceFiles
		moveServiceFiles(entry.Filename, trash, entry.name, allowCopy=False)

	def stopCurrentRecording(self, entry=-1):
		def confirm(answer=False):
			if answer:
				self.session.nav.RecordTimer.removeEntry(self.recording[entry])
				if self.deleteRecording:
					self.moveToTrash(self.recording[entry])
				self.recording.remove(self.recording[entry])
		if entry is not None and entry != -1:
			msg = _("Stop recording:")
			if self.deleteRecording:
				msg = _("Stop and delete recording:")
			msg += "\n"
			msg += " - " + self.recording[entry].name + "\n"
			self.session.openWithCallback(confirm, MessageBox, msg, MessageBox.TYPE_YESNO)

	def stopAllCurrentRecordings(self, list):
		def confirm(answer=False):
			if answer:
				for entry in list:
					self.session.nav.RecordTimer.removeEntry(entry[0])
					self.recording.remove(entry[0])
					if self.deleteRecording:
						self.moveToTrash(entry[0])
		msg = _("Stop recordings:")
		if self.deleteRecording:
			msg = _("Stop and delete recordings:")
		msg += "\n"
		for entry in list:
			msg += " - " + entry[0].name + "\n"
		self.session.openWithCallback(confirm, MessageBox, msg, MessageBox.TYPE_YESNO)

	def getProgramInfoAndEvent(self, info, name):
		info["serviceref"] = hasattr(self, "SelectedInstantServiceRef") and self.SelectedInstantServiceRef or self.session.nav.getCurrentlyPlayingServiceOrGroup()

		# Try to get event information.
		event = None
		try:
			epg = eEPGCache.getInstance()
			event = epg.lookupEventTime(info["serviceref"], -1, 0)
			if event is None:
				if hasattr(self, "SelectedInstantServiceRef") and self.SelectedInstantServiceRef:
					service_info = eServiceCenter.getInstance().info(self.SelectedInstantServiceRef)
					event = service_info and service_info.getEvent(self.SelectedInstantServiceRef)
				else:
					service = self.session.nav.getCurrentService()
					event = service and service.info().getEvent(0)
		except Exception:
			pass
		info["event"] = event
		info["name"] = name
		info["description"] = ""
		info["eventid"] = None
		if event is not None:
			curEvent = parseEvent(event)
			info["name"] = curEvent[2]
			info["description"] = curEvent[3]
			info["eventid"] = curEvent[4]
			info["end"] = curEvent[1]

	def startInstantRecording(self, limitEvent=""):
		begin = int(time())
		end = begin + 3600		# Dummy.
		name = _("Instant record")
		info = {}
		message = duration_message = ""
		timeout = 5
		added_timer = False
		self.getProgramInfoAndEvent(info, name)
		serviceref = info["serviceref"]
		event = info["event"]
		if limitEvent in ("event", "manualendtime", "manualduration"):
			if limitEvent in ("manualendtime", "manualduration") or (hasattr(self, "SelectedInstantServiceRef") and self.SelectedInstantServiceRef):
				message = _("Recording time has been set.")
			if event:
				end = info["end"]
			elif limitEvent == "event":
				message = _("No event information found, recording default is 24 hours.")
		if limitEvent in ("", "indefinitely"):
			message = _("Recording time has been set.")
			if event:
				info["name"] += " - " + name

		if isinstance(serviceref, eServiceReference):
			serviceref = ServiceReference(serviceref)
		recording = RecordTimerEntry(serviceref, begin, end, info["name"], info["description"], info["eventid"], dirname=preferredInstantRecordPath())
		recording.dontSave = True

		if not event or limitEvent in ("", "indefinitely"):
			recording.autoincrease = True
			recording.setAutoincreaseEnd()
			duration_message = "\n" + _("Default duration: %d mins") % ((recording.end - recording.begin) // 60) + "\n"
		simulTimerList = self.session.nav.RecordTimer.record(recording)

		if simulTimerList is None:  # No conflict.
			recording.autoincrease = False
			self.recording.append(recording)
			added_timer = True
		else:
			count = len(simulTimerList)
			if count > 1:  # With other recording.
				timeout = 10
				name = "'%s'" % simulTimerList[1].name
				name_date = " ".join((name, strftime("%F %T", localtime(simulTimerList[1].begin))))
				# print(f"[InfoBarGenerics] InstantTimer conflicts with {name_date}!")
				recording.autoincrease = True  # Start with max available length, then increment.
				if recording.setAutoincreaseEnd():
					self.session.nav.RecordTimer.record(recording)
					self.recording.append(recording)
					added_timer = True
					message += _("Record time limited due to conflicting timer: %s") % f"\n{name_date}"
					duration_message = "\n" + _("Default duration: %d mins") % ((recording.end - recording.begin) // 60) + "\n"
				else:
					message = _("Could not record due to conflicting timer: %s") % f"\n{name}"
					if count > 2:
						message += "\n" + _("total conflict (%d)") % (count - 1)
			else:
				ref = "\n'%s'" % serviceref
				message = _("Could not record due to invalid service %s") % ref
			recording.autoincrease = False
		if message:
			if added_timer and duration_message and limitEvent not in ("manualendtime", "manualduration"):
				message += duration_message
			self.session.open(MessageBox, text=message, type=MessageBox.TYPE_INFO, timeout=timeout, simple=True)
		return added_timer

	def startRecordingCurrentEvent(self):
		self.startInstantRecording(limitEvent="event")

	def isInstantRecordRunning(self):
		# print("[InfoBarGenerics] self.recording: {self.recording}")
		if self.recording:
			for x in self.recording:
				if x.isRunning():
					return True
		return False

	def recordQuestionCallback(self, answer):
		# print("[InfoBarGenerics] recordQuestionCallback")
		# print("[InfoBarGenerics] pre: {self.recording}")
		if answer is None or answer[1] == "no":
			return
		list = []
		recording = self.recording[:]
		for x in recording:
			if not x in self.session.nav.RecordTimer.timer_list:
				self.recording.remove(x)
			elif x.dontSave and x.isRunning():
				list.append((x, False))

		self.deleteRecording = False
		if answer[1] == "changeduration":
			if len(self.recording) == 1:
				self.changeDuration(0)
			else:
				self.session.openWithCallback(self.changeDuration, TimerSelection, list)
		elif answer[1] == "addrecordingtime":
			if len(self.recording) == 1:
				self.addRecordingTime(0)
			else:
				self.session.openWithCallback(self.addRecordingTime, TimerSelection, list)
		elif answer[1] == "changeendtime":
			if len(self.recording) == 1:
				self.setEndtime(0)
			else:
				self.session.openWithCallback(self.setEndtime, TimerSelection, list)
		elif answer[1] == "timer":
			from Screens.TimerEdit import TimerEditList
			self.session.open(TimerEditList)
		elif answer[1] == "stop":
			if len(self.recording) == 1:
				self.stopCurrentRecording(0)
			else:
				self.session.openWithCallback(self.stopCurrentRecording, TimerSelection, list)
		elif answer[1] == "stopdelete":
			self.deleteRecording = True
			if len(self.recording) == 1:
				self.stopCurrentRecording(0)
			else:
				self.session.openWithCallback(self.stopCurrentRecording, TimerSelection, list)
		elif answer[1] == "stopall":
			self.stopAllCurrentRecordings(list)
		elif answer[1] == "stopdeleteall":
			self.deleteRecording = True
			self.stopAllCurrentRecordings(list)
		elif answer[1] in ("indefinitely", "manualduration", "manualendtime", "event"):
			if self.startInstantRecording(limitEvent=answer[1]):
				if answer[1] == "manualduration":
					self.changeDuration(len(self.recording) - 1)
				elif answer[1] == "manualendtime":
					self.setEndtime(len(self.recording) - 1)
		elif "timeshift" in answer[1]:
			ts = self.getTimeshift()
			if ts:
				ts.saveTimeshiftFile()
				self.save_timeshift_file = True
				if "movie" in answer[1]:
					self.save_timeshift_in_movie_dir = True
				if "event" in answer[1]:
					remaining = self.currentEventTime()
					if remaining > 0:
						self.setCurrentEventTimer(remaining - 15)
		print("[InfoBarInstantRecord] after:\n", self.recording)

	def setEndtime(self, entry):
		if entry is not None and entry >= 0:
			self.selectedEntry = entry
			self.endtime = ConfigClock(default=self.recording[self.selectedEntry].end)
			dlg = self.session.openWithCallback(self.TimeDateInputClosed, TimeDateInput, self.endtime)
			dlg.setTitle(_("Please change recording endtime"))

	def TimeDateInputClosed(self, ret):
		if len(ret) > 1 and ret[0]:
			print("[InfoBarInstantRecord] stop recording at %s " % strftime("%F %T", localtime(ret[1])))
			entry = self.recording[self.selectedEntry]
			if entry.end != ret[1]:
				entry.autoincrease = False
			entry.end = ret[1]
			self.session.nav.RecordTimer.timeChanged(entry)

	def changeDuration(self, entry):
		if entry is not None and entry >= 0:
			self.selectedEntry = entry
			self.session.openWithCallback(self.inputCallback, InputBox, title=_("How many minutes do you want to record?"), text="5", maxSize=False, maxValue=1440, type=Input.NUMBER)

	def addRecordingTime(self, entry):
		if entry is not None and entry >= 0:
			self.selectedEntry = entry
			self.session.openWithCallback(self.inputAddRecordingTime, InputBox, title=_("How many minutes do you want add to the recording?"), text="5", maxSize=False, maxValue=1440, type=Input.NUMBER)

	def inputAddRecordingTime(self, value):
		if value:
			print("[InfoBarInstantRecord] added %d minutes for recording." % int(value))
			entry = self.recording[self.selectedEntry]
			if int(value) != 0:
				entry.autoincrease = False
			entry.end += 60 * int(value)
			self.session.nav.RecordTimer.timeChanged(entry)

	def inputCallback(self, value):
		if value:
			print("[InfoBarInstantRecord] stopping recording after %d minutes." % int(value))
			entry = self.recording[self.selectedEntry]
			if int(value) != 0:
				entry.autoincrease = False
			entry.end = int(time()) + 60 * int(value)
			self.session.nav.RecordTimer.timeChanged(entry)

	def isTimerRecordRunning(self):
		identical = timers = 0
		for timer in self.session.nav.RecordTimer.timer_list:
			if timer.isRunning() and not timer.justplay:
				timers += 1
				if self.recording:
					for x in self.recording:
						if x.isRunning() and x == timer:
							identical += 1
		return timers > identical

	def instantRecord(self, serviceRef=None):
		self.SelectedInstantServiceRef = serviceRef
		pirr = preferredInstantRecordPath()
		if not findSafeRecordPath(pirr) and not findSafeRecordPath(defaultMoviePath()):
			if not pirr:
				pirr = ""
			self.session.open(MessageBox, _("Missing ") + "\n" + pirr + "\n" + _("No HDD found or HDD not initialized!"), MessageBox.TYPE_ERROR)
			return

		if isStandardInfoBar(self):
			info = {}
			self.getProgramInfoAndEvent(info, "")
			event_entry = ((_("Add recording (Stop after current event)"), "event"),)
			common = ((_("Add recording (Indefinitely - 24 hours)"), "indefinitely"),
					(_("Add recording (Enter recording duration)"), "manualduration"),
					(_("Add recording (Enter recording end time)"), "manualendtime"),)
			if info["event"]:
				common = event_entry + common
		else:
			common = ()
		if self.isInstantRecordRunning():
			title = f'{_("A recording is currently running.")}\n{_("What do you want to do?")}'
			list = common + \
				((_("Change recording (Duration)"), "changeduration"),
				(_("Change recording (Add time)"), "addrecordingtime"),
				(_("Change recording (End time)"), "changeendtime"),)
			list += ((_("Stop recording"), "stop"),)
			if config.usage.movielist_trashcan.value:
				list += ((_("Stop and delete recording"), "stopdelete"),)
			if len(self.recording) > 1:
				list += ((_("Stop all current recordings"), "stopall"),)
				if config.usage.movielist_trashcan.value:
					list += ((_("Stop and delete all current recordings"), "stopdeleteall"),)
			if self.isTimerRecordRunning():
				list += ((_("Stop timer recording"), "timer"),)
			list += ((_("Do nothing"), "no"),)
		else:
			title = _("Start instant recording?")
			list = common
			if self.isTimerRecordRunning():
				list += ((_("Stop timer recording"), "timer"))
			if isStandardInfoBar(self):
				list += ((_("Do not record"), "no"),)
		if isStandardInfoBar(self) and self.timeshiftEnabled():
			list = list + ((_("Save timeshift file"), "timeshift"),
				(_("Save timeshift file in movie directory"), "timeshift_movie"))
			if self.currentEventTime() > 0:
				list += ((_("Save timeshift only for current event"), "timeshift_event"),)
		if list:
			self.session.openWithCallback(self.recordQuestionCallback, ChoiceBox, title=title, list=list)
		else:
			return 0


class InfoBarAudioSelection:
	def __init__(self):
		self["AudioSelectionAction"] = HelpableActionMap(self, "InfobarAudioSelectionActions", {
			"audioSelection": (self.audioSelection, _("Audio options...")),
			"yellow_key": (self.yellow_key, _("Audio options...")),
			"audioSelectionLong": (self.audioDownmixToggle, _("Toggle Digital downmix...")),
		}, prio=0, description=_("Audio Actions"))

	def yellow_key(self):
		from Screens.AudioSelection import AudioSelection
		self.session.openWithCallback(self.audioSelected, AudioSelection, infobar=self)

	def audioSelection(self):
		from Screens.AudioSelection import AudioSelection
		self.session.openWithCallback(self.audioSelected, AudioSelection, infobar=self)

	def audioSelected(self, ret=None):
		print("[InfoBarGenerics] [infobar::audioSelected]", ret)

	def audioDownmixToggle(self, popup=True):
		if BoxInfo.getItem("CanDownmixAC3"):
			if config.av.downmix_ac3.value:
				message = _("Dolby Digital downmix is now") + " " + _("disabled")
				print("[InfoBarGenerics] [Audio] Dolby Digital downmix is now disabled")
				config.av.downmix_ac3.setValue(False)
			else:
				config.av.downmix_ac3.setValue(True)
				message = _("Dolby Digital downmix is now") + " " + _("enabled")
				print("[InfoBarGenerics] [Audio] Dolby Digital downmix is now enabled")
			if popup:
				Notifications.AddPopup(text=message, type=MessageBox.TYPE_INFO, timeout=5, id="DDdownmixToggle")

	def audioDownmixOn(self):
		if not config.av.downmix_ac3.value:
			self.audioDownmixToggle(False)

	def audioDownmixOff(self):
		if config.av.downmix_ac3.value:
			self.audioDownmixToggle(False)


# Subservice processing.
#
instanceInfoBarSubserviceSelection = None


class InfoBarSubserviceSelection:
	def __init__(self):
		global instanceInfoBarSubserviceSelection
		instanceInfoBarSubserviceSelection = self
		self.subservicesGroups = self.loadSubservicesGroups()
		self["SubserviceSelectionAction"] = HelpableActionMap(self, ["InfobarSubserviceSelectionActions"],
			{
				"subserviceSelection": (self.subserviceSelection, _("Subservice list...")),
			}, prio=0, description=_("Subservice Actions"))

		self["SubserviceQuickzapAction"] = HelpableActionMap(self, ["InfobarSubserviceQuickzapActions"],
			{
				"nextSubservice": (self.nextSubservice, _("Switch to next sub service")),
				"prevSubservice": (self.prevSubservice, _("Switch to previous sub service"))
			}, -10)
		self["SubserviceQuickzapAction"].setEnabled(False)

		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evUpdatedEventInfo: self.checkSubservicesAvail
			})
		self.onClose.append(self.__removeNotifications)

		self.bouquets = self.bsel = self.selectedSubservice = None

	def __removeNotifications(self):
		self.session.nav.event.remove(self.checkSubservicesAvail)

	def loadSubservicesGroups(self):
		subservicesGroups = []
		groupedServicesFile = resolveFilename(SCOPE_CONFIG, "groupedservices")
		if not isfile(groupedServicesFile):
			groupedServicesFile = resolveFilename(SCOPE_SKINS, "groupedservices")
			if not isfile(groupedServicesFile):
				groupedServicesFile = None
				print("[InfoBarGenerics] No 'groupedservices' file found so no subservices are available.")
		if groupedServicesFile:
			subservicesGroups = [list(g) for k, g in itertools.groupby([line.split("#")[0].strip() for line in fileReadLines(groupedServicesFile, [], source=MODULE_NAME)], lambda x: not x) if not k]
			count = len(subservicesGroups)
			print(f"[InfoBarGenerics] {count} subservice group{'' if count == 1 else 's'} loaded from '{groupedServicesFile}'.")
		return subservicesGroups

	def getSubserviceGroups(self):
		return self.subservicesGroups

	def hasActiveSubservicesForCurrentService(self, serviceReference):
		if serviceReference and "%3a" not in serviceReference:
			serviceReference = ":".join(serviceReference.split(":")[:11])
		if config.usage.showInfoBarSubservices.value == 1:
			subservices = self.getActiveSubservicesForCurrentService(serviceReference)
		elif config.usage.showInfoBarSubservices.value == 2:
			subservices = self.getPossibleSubservicesForCurrentService(serviceReference)
		else:
			subservices = None
		return bool(subservices and len(subservices) > 1)

	def getActiveSubservicesForCurrentService(self, serviceReference):
		transmissionPaused = [
			"Sendepause"  # This is for German TV.
		]
		activeSubservices = []
		if config.usage.showInfoBarSubservices.value and serviceReference:
			possibleSubservices = self.getPossibleSubservicesForCurrentService(serviceReference)
			epgCache = eEPGCache.getInstance()
			for subservice in possibleSubservices:
				events = epgCache.lookupEvent(["T", (subservice, 0, -1)])
				if events and len(events) == 1:
					title = events[0][0]
					if title and not any([x for x in transmissionPaused if x in title]):
						activeSubservices.append(subservice)
				elif config.usage.showInfoBarSubservices.value == 2:
					activeSubservices.append(subservice)
		return activeSubservices

	def getPossibleSubservicesForCurrentService(self, serviceReference):
		possibleSubservices = []
		if serviceReference and self.subservicesGroups:
			possibleSubserviceGroups = [x for x in self.subservicesGroups if serviceReference in x]
			if possibleSubserviceGroups:
				possibleSubservices = possibleSubserviceGroups[0]  # If the service is in multiple groups should we return more options?
		return possibleSubservices

	def checkSubservicesAvail(self):
		serviceRef = self.session.nav.getCurrentlyPlayingServiceReference()
		service = self.session.nav.getCurrentService()
		if not serviceRef or not hasActiveSubservicesForCurrentChannel(service):
			self["SubserviceQuickzapAction"].setEnabled(False)
			self.bouquets = self.bsel = self.selectedSubservice = None

	def nextSubservice(self):
		self.changeSubservice(+1)

	def prevSubservice(self):
		self.changeSubservice(-1)

	def playSubservice(self, ref):
		if ref.getUnsignedData(6) == 0:
			ref.setName("")
		self.session.nav.playService(ref, checkParentalControl=False, adjust=False)

	def changeSubservice(self, direction):
		serviceRef = self.session.nav.getCurrentlyPlayingServiceReference()
		if serviceRef:
			service = self.session.nav.getCurrentService()
			subservices = getActiveSubservicesForCurrentChannel(service)
			if subservices and len(subservices) >= 2 and serviceRef.toString() in [x[1] for x in subservices]:
				selection = [x[1] for x in subservices].index(serviceRef.toString())
				selection += direction % len(subservices)
				try:
					newservice = eServiceReference(subservices[selection][0])
				except:
					newservice = None
				if newservice and newservice.valid():
					self.playSubservice(newservice)

	def subserviceSelection(self):
		serviceRef = self.session.nav.getCurrentlyPlayingServiceReference()
		if serviceRef:
			service = self.session.nav.getCurrentService()
			subservices = getActiveSubservicesForCurrentChannel(service)
			if subservices and len(subservices) >= 2 and (serviceRef.toString() in [x[1] for x in subservices] or service.subServices()):
				try:
					selection = [x[1] for x in subservices].index(serviceRef.toString())
				except:
					selection = 0
				self.bouquets = self.servicelist and self.servicelist.getBouquetList()
				tlist = None
				if self.bouquets and len(self.bouquets):
					keys = ["red", "blue", "", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
					call_func_title = _("Add to favourites")
					if config.usage.multibouquet.value:
						call_func_title = _("Add to bouquet")
						tlist = [(_("Quick zap"), "quickzap", subservices), (call_func_title, "CALLFUNC", self.addSubserviceToBouquetCallback), ("--", "")] + subservices
					selection += 3
				else:
					tlist = [(_("Quick zap"), "quickzap", subservices), ("--", "")] + subservices
					keys = ["red", "", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
					selection += 2
				if tlist:
					self.session.openWithCallback(self.subserviceSelected, ChoiceBox, title=_("Please select a sub service..."), list=tlist, selection=selection, keys=keys, skin_name="SubserviceSelection")
				else:
					self.session.open(MessageBox, _("No active subservices available."), MessageBox.TYPE_INFO, timeout=5, simple=True)

	def subserviceSelected(self, service):
		if service and len(service) > 1:
			if service[1] == "quickzap":
				from Screens.SubservicesQuickzap import SubservicesQuickzap
				self.session.open(SubservicesQuickzap, service[2])
			else:
				try:
					ref = eServiceReference(service[1])
				except:
					ref = None
				if ref and ref.valid():
					self["SubserviceQuickzapAction"].setEnabled(True)
					self.playSubservice(ref)

	def addSubserviceToBouquetCallback(self, service):
		if service and len(service) > 1:
			try:
				self.selectedSubservice = eServiceReference(service[1])
			except:
				self.selectedSubservice = None
			if self.selectedSubservice is None or not self.selectedSubservice.valid() or self.bouquets is None:
				self.bouquets = self.bsel = self.selectedSubservice = None
				return
			cnt = len(self.bouquets)
			if cnt > 1:
				self.bsel = self.session.openWithCallback(self.bouquetSelClosed, BouquetSelector, self.bouquets, self.addSubserviceToBouquet)
			elif cnt == 1:
				self.addSubserviceToBouquet(self.bouquets[0][1])
				self.session.open(MessageBox, _("Service has been added to the favourites."), MessageBox.TYPE_INFO, timeout=5)

	def bouquetSelClosed(self, confirmed):
		self.bouquets = self.bsel = self.selectedSubservice = None
		if confirmed:
			self.session.open(MessageBox, _("Service has been added to the selected bouquet."), MessageBox.TYPE_INFO, timeout=5)

	def addSubserviceToBouquet(self, dest):
		self.servicelist.addServiceToBouquet(dest, self.selectedSubservice)
		if self.bsel:
			self.bsel.close(True)
			self.bouquets = self.bsel = self.selectedSubservice = None

from Components.Sources.HbbtvApplication import HbbtvApplication
gHbbtvApplication = HbbtvApplication()


class InfoBarRedButton:
	def __init__(self):
		self["RedButtonActions"] = HelpableActionMap(self, ["InfobarRedButtonActions"], {
			"activateRedButton": (self.activateRedButton, _("Red button...")),
		}, prio=0, description=_("Red/HbbTV Button Actions"))
		self["HbbtvApplication"] = gHbbtvApplication
		self.onHBBTVActivation = []
		self.onRedButtonActivation = []
		self.onReadyForAIT = []
		self.__et = ServiceEventTracker(screen=self, eventmap={
			iPlayableService.evHBBTVInfo: self.detectedHbbtvApplication,
			iPlayableService.evUpdatedInfo: self.updateInfomation
		})

	def updateAIT(self, orgId=0):
		for x in self.onReadyForAIT:
			try:
				x(orgId)
			except Exception as ErrMsg:
				print("[InfoBarGenerics] updateAIT error", ErrMsg)
				# self.onReadyForAIT.remove(x)

	def updateInfomation(self):
		try:
			self["HbbtvApplication"].setApplicationName("")
			self.updateAIT()
		except Exception as ErrMsg:
			pass

	def detectedHbbtvApplication(self):
		service = self.session.nav.getCurrentService()
		info = service and service.info()
		try:
			for x in info.getInfoObject(iServiceInformation.sHBBTVUrl):
				print("[InfoBarGenerics] HbbtvApplication:", x)
				if x[0] in (-1, 1):
					self.updateAIT(x[3])
					self["HbbtvApplication"].setApplicationName(x[1])
					break
		except Exception as ErrMsg:
			pass

	def activateRedButton(self):
		service = self.session.nav.getCurrentService()
		info = service and service.info()
		if info and info.getInfoString(iServiceInformation.sHBBTVUrl) != "":
			for x in self.onHBBTVActivation:
				x()
		elif False:  # TODO: other red button services
			for x in self.onRedButtonActivation:
				x()


class InfoBarAspectSelection:
	STATE_HIDDEN = 0
	STATE_ASPECT = 1
	STATE_RESOLUTION = 2

	def __init__(self):
		self["AspectSelectionAction"] = HelpableActionMap(self, ["InfobarAspectSelectionActions"], {
			"aspectSelection": (self.ExGreen_toggleGreen, _("Aspect list...")),
			"exitLong": (self.switchTo720p, _("Switch to 720p video?")),
		}, prio=0, description=_("Aspect Ratio Actions"))
		self.__ExGreen_state = self.STATE_HIDDEN

	def ExGreen_doAspect(self):
		print("[InfoBarGenerics] do self.STATE_ASPECT")
		self.__ExGreen_state = self.STATE_ASPECT
		self.aspectSelection()

	def ExGreen_doResolution(self):
		print("[InfoBarGenerics] do self.STATE_RESOLUTION")
		self.__ExGreen_state = self.STATE_RESOLUTION
		self.resolutionSelection()

	def ExGreen_doHide(self):
		print("[InfoBarGenerics] do self.STATE_HIDDEN")
		self.__ExGreen_state = self.STATE_HIDDEN

	def ExGreen_toggleGreen(self, arg=""):
		print("[InfoBarGenerics] toggleGreen:", self.__ExGreen_state)
		if self.__ExGreen_state == self.STATE_HIDDEN:
			print("[InfoBarGenerics] self.STATE_HIDDEN")
			self.ExGreen_doAspect()
		elif self.__ExGreen_state == self.STATE_ASPECT:
			print("[InfoBarGenerics] self.STATE_ASPECT")
			self.ExGreen_doResolution()
		elif self.__ExGreen_state == self.STATE_RESOLUTION:
			print("[InfoBarGenerics] self.STATE_RESOLUTION")
			self.ExGreen_doHide()

	def aspectSelection(self):
		selection = 0
		if BoxInfo.getItem("AmlogicFamily"):
			aspectList = [
				(_("Resolution"), "resolution"),
				("--", ""),
				(_("Normal"), "0"),
				(_("Full Stretch"), "1"),
				("4:3", "2"),
				("16:9", "3"),
				(_("Non-Linear"), "4"),
				(_("Normal No ScaleUp"), "5"),
				(_("4:3 Ignore"), "6"),
				(_("4:3 Letterbox"), "7"),
				(_("4:3 PanScan"), "8"),
				(_("4:3 Combined"), "9"),
				(_("16:9 Ignore"), "10"),
				(_("16:9 Letterbox"), "11"),
				(_("16:9 PanScan"), "12"),
				(_("16:9 Combined"), "13")
			]
		else:
			aspectSwitchList = []
			if config.av.aspectswitch.enabled.value:
				for aspect in range(5):
					aspectSwitchList.append((avSwitch.ASPECT_SWITCH_MSG[aspect], str(aspect + 100)))
				aspectSwitchList.append(("--", ""))
			aspectList = [
				(_("Resolution"), "resolution"),
				("--", "")
			] + aspectSwitchList + [
				(_("4:3 Letterbox"), "0"),
				(_("4:3 PanScan"), "1"),
				(_("16:9"), "2"),
				(_("16:9 Always"), "3"),
				(_("16:10 Letterbox"), "4"),
				(_("16:10 PanScan"), "5"),
				(_("16:9 Letterbox"), "6")
			]
		keys = ["green", "", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
		aspect = avSwitch.getAspectRatioSetting()
		selection = 0
		for item in range(len(aspectList)):
			if aspectList[item][1] == aspect:
				selection = item
				break
		self.session.openWithCallback(self.aspectSelected, ChoiceBox, title=_("Please select an aspect ratio..."), list=aspectList, keys=keys, selection=selection)

	def changeVideoMode(self, confirmed):
		from Plugins.SystemPlugins.Videomode.VideoHardware import video_hw
		port = config.av.videoport.value
		mode = config.av.videomode[port].value
		rate = config.av.videorate[mode].value
		self.last_used_video_mode = (port, mode, rate)  # have video 720p
		if not confirmed:   # return to the last used video mode
			config.av.videoport.value = self.last_used_video_mode[0]
			config.av.videomode[self.last_used_video_mode[0]].value = self.last_used_video_mode[1]
			config.av.videorate[self.last_used_video_mode[1]].value = self.last_used_video_mode[2]
			video_hw.setMode(*self.last_used_video_mode)

	def switchTo720p(self):  # use 720p video mode recover signal on your video port
		from Plugins.SystemPlugins.Videomode.VideoHardware import video_hw
		video_hw.setMode("HDMI", "720p", "50Hz")
		self.session.openWithCallback(self.changeVideoMode, MessageBox, _("This function recovers your video signal in case of loss. The video has been changed to 720p.\nIf this is your case, please keep the video at 720P and do the following:\nGo to Menu > Setup > Audio / Video > A/V settings and set the correct resolution.\nDo you want to keep the video at 720p?"), MessageBox.TYPE_YESNO, timeout=30, simple=True)

	def aspectSelected(self, aspect):
		if aspect is not None:
			if isinstance(aspect[1], str):
				if aspect[1] == "":
					self.ExGreen_doHide()
				elif aspect[1] == "resolution":
					self.ExGreen_toggleGreen()
				else:
					avSwitch.setAspectRatio(int(aspect[1]))
					self.ExGreen_doHide()
		else:
			self.ExGreen_doHide()
		return


class InfoBarResolutionSelection:
	def __init__(self):
		pass

	def resolutionSelection(self):
		avControl = eAVControl.getInstance()
		fps = float(avControl.getFrameRate(50000)) / 1000.0
		yRes = avControl.getResolutionY(0)
		xRes = avControl.getResolutionX(0)
		resList = []
		resList.append((_("Exit"), "exit"))
		resList.append((_("Auto(not available)"), "auto"))
		resList.append((_("Video") + ": %dx%d@%gHz" % (xRes, yRes, fps), ""))
		resList.append(("--", ""))
		# Do we need a new sorting with this way here or should we disable some choices?
		modes = eAVControl.getInstance().getAvailableModes()
		videoModes = modes.split()
		for videoMode in videoModes:
			video = videoMode
			if videoMode.endswith("23"):
				video = "%s.976" % videoMode
			if videoMode[-1].isdigit():
				video = "%sHz" % videoMode
			resList.append((video, videoMode))
		videoMode = avControl.getVideoMode("Unknown")
		keys = ["green", "yellow", "blue", "", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
		selection = 0
		for index, item in enumerate(resList):
			if item[1] == videoMode:
				selection = index
				break
		print("[InfoBarGenerics] Current video mode is %s." % videoMode)
		self.session.openWithCallback(self.resolutionSelected, ChoiceBox, title=_("Please select a resolution..."), list=resList, keys=keys, selection=selection)

	def resolutionSelected(self, videoMode):
		if videoMode is not None:
			if isinstance(videoMode[1], str):
				if videoMode[1] == "exit" or videoMode[1] == "" or videoMode[1] == "auto":
					self.ExGreen_toggleGreen()
				if videoMode[1] != "auto":
					avSwitch.setVideoModeDirect(videoMode[1])
					self.ExGreen_doHide()
		else:
			self.ExGreen_doHide()
		return


class InfoBarTimerButton:
	def __init__(self):
		self["TimerButtonActions"] = HelpableActionMap(self, ["InfobarTimerButtonActions"],
			{
				"timerSelection": (self.timerSelection, _("Timer selection")),
			})

	def timerSelection(self):
		from Screens.TimerEdit import TimerEditList
		self.session.open(TimerEditList)


class VideoMode(Screen):
	def __init__(self, session):
		Screen.__init__(self, session)
		self["videomode"] = Label()
		self.timer = eTimer()
		self.timer.callback.append(self.hide)

	def setText(self, text=""):
		self["videomode"].setText(text)
		self.show()
		self.timer.startLongTimer(3)


class InfoBarVmodeButton:
	def __init__(self):
		self["VmodeButtonActions"] = HelpableActionMap(self, ["InfobarVmodeButtonActions"],
			{
				"vmodeSelection": (self.ToggleVideoMode, _("Letterbox zoom")),
			})
		self.VideoMode_window = self.session.instantiateDialog(VideoMode)

	def ToggleVideoMode(self):
		policy = config.av.policy_169 if self.isWideScreen() else config.av.policy_43
		policy.value = policy.choices[(policy.choices.index(policy.value) + 1) % len(policy.choices)]
		self.VideoMode_window.setText(policy.value)
		config.av.policy_169.save()
		config.av.policy_43.save()

	def isWideScreen(self):
		from Components.Converter.ServiceInfo import WIDESCREEN
		service = self.session.nav.getCurrentService()
		info = service and service.info()
		return info and info.getInfo(iServiceInformation.sAspect) in WIDESCREEN


class InfoBarAdditionalInfo:
	def __init__(self):

		self["RecordingPossible"] = Boolean(fixed=harddiskmanager.HDDCount() > 0)
		self["TimeshiftPossible"] = self["RecordingPossible"]
		self["ExtensionsAvailable"] = Boolean(fixed=1)
		# TODO: These properties should be queried from the input device keymap.
		self["ShowTimeshiftOnYellow"] = Boolean(fixed=0)
		self["ShowAudioOnYellow"] = Boolean(fixed=0)
		self["ShowRecordOnRed"] = Boolean(fixed=0)


class InfoBarNotifications:
	def __init__(self):
		self.onExecBegin.append(self.checkNotifications)
		notificationAdded.append(self.checkNotificationsIfExecing)
		self.onClose.append(self.__removeNotification)

	def __removeNotification(self):
		notificationAdded.remove(self.checkNotificationsIfExecing)

	def checkNotificationsIfExecing(self):
		if self.execing:
			self.checkNotifications()

	def checkNotifications(self):
		lock.acquire(True)
		my_notifications = notifications
		n = my_notifications and my_notifications[0]
		if n:
			del my_notifications[0]
		lock.release()
		if n:
			cb = n[0]

			if "onSessionOpenCallback" in n[3]:
				n[3]["onSessionOpenCallback"]()
				del n[3]["onSessionOpenCallback"]

			if n[4] and n[4].startswith("ChannelsImport"):
				if "channels" in config.usage.remote_fallback_import.value:
					eDVBDB.getInstance().reloadBouquets()
					eDVBDB.getInstance().reloadServicelist()
					from Components.ParentalControl import parentalControl
					parentalControl.open()
					refreshServiceList()
					reload_whitelist_vbi()
				if "epg" in config.usage.remote_fallback_import.value:
					eEPGCache.getInstance().load()
				if config.misc.initialchannelselection.value or not (config.usage.remote_fallback_import.value and (n[4].endswith("NOK") and config.usage.remote_fallback_nok.value or config.usage.remote_fallback_ok.value)):
					return
			if cb:
				dlg = self.session.openWithCallback(cb, n[1], *n[2], **n[3])
			elif not current_notifications and n[4] == "ZapError":
				if "timeout" in n[3]:
					del n[3]["timeout"]
				n[3]["enable_input"] = False
				dlg = self.session.instantiateDialog(n[1], *n[2], **n[3])
				self.hide()
				dlg.show()
				self.notificationDialog = dlg
				eActionMap.getInstance().bindAction("", -maxsize - 1, self.keypressNotification)
			else:
				dlg = self.session.open(n[1], *n[2], **n[3])

			# Remember that this notification is currently active.
			d = (n[4], dlg)
			current_notifications.append(d)
			dlg.onClose.append(boundFunction(self.__notificationClosed, d))

	def closeNotificationInstantiateDialog(self):
		if hasattr(self, "notificationDialog"):
			self.session.deleteDialog(self.notificationDialog)
			del self.notificationDialog
			eActionMap.getInstance().unbindAction("", self.keypressNotification)

	def keypressNotification(self, key, flag):
		if flag:
			self.closeNotificationInstantiateDialog()

	def __notificationClosed(self, d):
		current_notifications.remove(d)


class InfoBarServiceNotifications:
	def __init__(self):
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evEnd: self.serviceHasEnded
			})

	def serviceHasEnded(self):
		print("[InfoBarGenerics] service end!")

		try:
			self.setSeekState(self.SEEK_STATE_PLAY)
		except Exception:
			pass


class InfoBarCueSheetSupport:
	CUT_TYPE_IN = 0
	CUT_TYPE_OUT = 1
	CUT_TYPE_MARK = 2
	CUT_TYPE_LAST = 3

	ENABLE_RESUME_SUPPORT = False

	def __init__(self, actionmap=["InfobarCueSheetActions"]):
		self["CueSheetActions"] = HelpableActionMap(self, actionmap,
			{
				"jumpPreviousMark": (self.jumpPreviousMark, _("Jump to previous marked position")),
				"jumpNextMark": (self.jumpNextMark, _("Jump to next marked position")),
				"toggleMark": (self.toggleMark, _("Toggle a cut mark at the current position"))
			}, prio=1)

		self.cut_list = []
		self.is_closing = False
		self.resumeTimer = eTimer()
		self.resumeTimer.callback.append(self.triggerResumeLogic)
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evStart: self.__serviceStarted,
				iPlayableService.evCuesheetChanged: self.downloadCuesheet,
			})

	def __serviceStarted(self):
		self.resumeTimer.stop()
		self.resumeTimer.start(1000, True)

	def triggerResumeLogic(self):
		if self.is_closing:
			return
		print("[InfoBarGenerics] new service started! trying to download cuts!")
		self.downloadCuesheet()

		if self.ENABLE_RESUME_SUPPORT:
			for (pts, what) in self.cut_list:
				if what == self.CUT_TYPE_LAST:
					last = pts
					break
			else:
				last = resumePointsInstance.getResumePoint(self.session)
			if last is None:
				return
			# Only resume if at least 10 seconds ahead, or <10 seconds before the end.
			seekable = self.__getSeekable()
			if seekable is None:
				return  # Should not happen?
			length = seekable.getLength()
			if length[0]:
				length = (-1, 0)  # Set length 0 if error in getLength()
			print("[InfoBarGenerics] seekable.getLength() returns:", length)
			if (last > 900000) and (not length[1] or last < length[1] - 900000):
				self.resume_point = last
				l = last // 90000
				if "ask" in config.usage.on_movie_start.value:
					AddNotificationWithCallback(self.playLastCB, MessageBox, _("Do you want to resume this playback?") + "\n" + (_("Resume position at %s") % ("%d:%02d:%02d" % (l / 3600, l % 3600 / 60, l % 60))), timeout=10, default="yes" in config.usage.on_movie_start.value)
				elif config.usage.on_movie_start.value == "resume":
					# TRANSLATORS: The string "Resuming playback" flashes for a moment
					# TRANSLATORS: at the start of a movie, when the user has selected
					# TRANSLATORS: "Resume from last position" as start behavior.
					# TRANSLATORS: The purpose is to notify the user that the movie starts
					# TRANSLATORS: in the middle somewhere and not from the beginning.
					# TRANSLATORS: (Some translators seem to have interpreted it as a
					# TRANSLATORS: question or a choice, but it is a statement.)
					AddNotificationWithCallback(self.playLastCB, MessageBox, _("Resuming playback"), timeout=2, type=MessageBox.TYPE_INFO)

	def playLastCB(self, answer):
		if answer == True:
			self.doSeek(self.resume_point)
		self.hideAfterResume()

	def hideAfterResume(self):
		if isinstance(self, InfoBarShowHide):
			self.hide()

	def __getSeekable(self):
		service = self.session.nav.getCurrentService()
		if service is None:
			return None
		return service.seek()

	def cueGetCurrentPosition(self):
		seek = self.__getSeekable()
		if seek is None:
			return None
		r = seek.getPlayPosition()
		if r[0]:
			return None
		return int(r[1])

	def cueGetEndCutPosition(self):
		ret = False
		isin = True
		for cp in self.cut_list:
			if cp[1] == self.CUT_TYPE_OUT:
				if isin:
					isin = False
					ret = cp[0]
			elif cp[1] == self.CUT_TYPE_IN:
				isin = True
		return ret

	def jumpPreviousNextMark(self, cmp, start=False):
		current_pos = self.cueGetCurrentPosition()
		if current_pos is None:
			return False
		mark = self.getNearestCutPoint(current_pos, cmp=cmp, start=start)
		if mark is not None:
			pts = mark[0]
		else:
			return False

		self.doSeek(pts)
		return True

	def jumpPreviousMark(self):
		# We add 5 seconds, so if the play position is <5s after
		# the mark, the mark before will be used.
		self.jumpPreviousNextMark(lambda x: -x - 5 * 90000, start=True)

	def jumpNextMark(self):
		if not self.jumpPreviousNextMark(lambda x: x - 90000):
			self.doSeek(-1)

	def getNearestCutPoint(self, pts, cmp=abs, start=False):
		# Can be optimized.
		beforecut = True
		nearest = None
		bestdiff = -1
		instate = True
		if start:
			bestdiff = cmp(0 - pts)
			if bestdiff >= 0:
				nearest = [0, False]
		for cp in self.cut_list:
			if beforecut and cp[1] in (self.CUT_TYPE_IN, self.CUT_TYPE_OUT):
				beforecut = False
				if cp[1] == self.CUT_TYPE_IN:  # Start is here, disregard previous marks.
					diff = cmp(cp[0] - pts)
					if start and diff >= 0:
						nearest = cp
						bestdiff = diff
					else:
						nearest = None
						bestdiff = -1
			if cp[1] == self.CUT_TYPE_IN:
				instate = True
			elif cp[1] == self.CUT_TYPE_OUT:
				instate = False
			elif cp[1] in (self.CUT_TYPE_MARK, self.CUT_TYPE_LAST):
				diff = cmp(cp[0] - pts)
				if instate and diff >= 0 and (nearest is None or bestdiff > diff):
					nearest = cp
					bestdiff = diff
		return nearest

	def toggleMark(self, onlyremove=False, onlyadd=False, tolerance=5 * 90000, onlyreturn=False):
		current_pos = self.cueGetCurrentPosition()
		if current_pos is None:
			print("[InfoBarGenerics] not seekable")
			return

		nearest_cutpoint = self.getNearestCutPoint(current_pos)

		if nearest_cutpoint is not None and abs(nearest_cutpoint[0] - current_pos) < tolerance:
			if onlyreturn:
				return nearest_cutpoint
			if not onlyadd:
				self.removeMark(nearest_cutpoint)
		elif not onlyremove and not onlyreturn:
			self.addMark((current_pos, self.CUT_TYPE_MARK))

		if onlyreturn:
			return None

	def addMark(self, point):
		insort(self.cut_list, point)
		self.uploadCuesheet()
		self.showAfterCuesheetOperation()

	def removeMark(self, point):
		self.cut_list.remove(point)
		self.uploadCuesheet()
		self.showAfterCuesheetOperation()

	def showAfterCuesheetOperation(self):
		if isinstance(self, InfoBarShowHide):
			self.doShow()

	def __getCuesheet(self):
		service = self.session.nav.getCurrentService()
		if service is None:
			return None
		return service.cueSheet()

	def uploadCuesheet(self):
		cue = self.__getCuesheet()

		if cue is None:
			print("[InfoBarGenerics] upload failed, no cuesheet interface")
			return
		cue.setCutList(self.cut_list)

	def downloadCuesheet(self):
		cue = self.__getCuesheet()

		if cue is None:
			print("[InfoBarGenerics] download failed, no cuesheet interface")
			self.cut_list = []
		else:
			self.cut_list = cue.getCutList()


class InfoBarSummary(Screen):
	skin = """
	<screen position="0,0" size="132,64">
		<widget source="global.CurrentTime" render="Label" position="62,46" size="82,18" font="Regular;16" >
			<convert type="ClockToText">WithSeconds</convert>
		</widget>
		<widget source="session.RecordState" render="FixedLabel" text=" " position="62,46" size="82,18" zPosition="1" >
			<convert type="ConfigEntryTest">config.usage.blinking_display_clock_during_recording,True,CheckSourceBoolean</convert>
			<convert type="ConditionalShowHide">Blink</convert>
		</widget>
		<widget source="session.CurrentService" render="Label" position="6,4" size="120,42" font="Regular;18" >
			<convert type="ServiceName">Name</convert>
		</widget>
		<widget source="session.Event_Now" render="Progress" position="6,46" size="46,18" borderWidth="1" >
			<convert type="EventTime">Progress</convert>
		</widget>
	</screen>"""

# For picon:  (path="piconlcd" will use LCD picons)
# 		<widget source="session.CurrentService" render="Picon" position="6,0" size="120,64" path="piconlcd" >
# 			<convert type="ServiceName">Reference</convert>
# 		</widget>


class InfoBarSummarySupport:
	def __init__(self):
		pass

	def createSummary(self):
		return InfoBarSummary


class InfoBarMoviePlayerSummary(Screen):
	skin = """
	<screen position="0,0" size="132,64">
		<widget source="global.CurrentTime" render="Label" position="62,46" size="64,18" font="Regular;16" horizontalAlignment="right" >
			<convert type="ClockToText">WithSeconds</convert>
		</widget>
		<widget source="session.RecordState" render="FixedLabel" text=" " position="62,46" size="64,18" zPosition="1" >
			<convert type="ConfigEntryTest">config.usage.blinking_display_clock_during_recording,True,CheckSourceBoolean</convert>
			<convert type="ConditionalShowHide">Blink</convert>
		</widget>
		<widget source="session.CurrentService" render="Label" position="6,4" size="120,42" font="Regular;18" >
			<convert type="ServiceName">Name</convert>
		</widget>
		<widget source="session.CurrentService" render="Progress" position="6,46" size="56,18" borderWidth="1" >
			<convert type="ServicePosition">Position</convert>
		</widget>
	</screen>"""


class InfoBarMoviePlayerSummarySupport:
	def __init__(self):
		pass

	def createSummary(self):
		return InfoBarMoviePlayerSummary


class InfoBarTeletextPlugin:
	def __init__(self):
		self.teletext_plugin = None

		for p in plugins.getPlugins(PluginDescriptor.WHERE_TELETEXT):
			self.teletext_plugin = p

		if self.teletext_plugin is not None:
			self["TeletextActions"] = HelpableActionMap(self, ["InfobarTeletextActions"],
				{
					"startTeletext": (self.startTeletext, _("View teletext..."))
				})
		else:
			print("[InfoBarGenerics] no teletext plugin found!")

	def startTeletext(self):
		self.teletext_plugin and self.teletext_plugin(session=self.session, service=self.session.nav.getCurrentService())


class InfoBarSubtitleSupport:
	def __init__(self):
		object.__init__(self)
		self["SubtitleSelectionAction"] = HelpableActionMap(self, ["InfobarSubtitleSelectionActions"],
			{
				"subtitleSelection": (self.subtitleSelection, _("Subtitle selection...")),
				"subtitleShowHide": (self.toggleSubtitleShown, _("Subtitle show/hide...")),
			})

		self.selected_subtitle = None

		if isStandardInfoBar(self):
			self.subtitle_window = self.session.instantiateDialog(SubtitleDisplay)
			self.subtitle_window.setAnimationMode(0)
		else:
			from Screens.InfoBar import InfoBar
			self.subtitle_window = InfoBar.instance.subtitle_window

		self.subtitle_window.hide()
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evStart: self.__serviceChanged,
				iPlayableService.evEnd: self.__serviceChanged,
				iPlayableService.evUpdatedInfo: self.__updatedInfo
			})

	def getCurrentServiceSubtitle(self):
		service = self.session.nav.getCurrentService()
		return service and service.subtitle()

	def subtitleSelection(self):
		subtitle = self.getCurrentServiceSubtitle()
		subtitlelist = subtitle and subtitle.getSubtitleList()
		if self.selected_subtitle or subtitlelist and len(subtitlelist) > 0:
			from Screens.AudioSelection import SubtitleSelection
			self.session.open(SubtitleSelection, self)
		else:
			return 0

	def subtitleQuickMenu(self):
		service = self.session.nav.getCurrentService()
		subtitle = service and service.subtitle()
		subtitlelist = subtitle and subtitle.getSubtitleList()
		if self.selected_subtitle and self.selected_subtitle != (0, 0, 0, 0):
			from Screens.AudioSelection import QuickSubtitlesConfigMenu
			self.session.open(QuickSubtitlesConfigMenu, self)
		else:
			self.subtitleSelection()

	def doCenterDVBSubs(self):
		service = self.session.nav.getCurrentlyPlayingServiceReference()
		servicepath = service and service.getPath()
		if servicepath and servicepath.startswith("/"):
			if service.toString().startswith("1:"):
				info = eServiceCenter.getInstance().info(service)
				service = info and info.getInfoString(service, iServiceInformation.sServiceref)
				config.subtitles.dvb_subtitles_centered.value = service and eDVBDB.getInstance().getFlag(eServiceReference(service)) & self.FLAG_CENTER_DVB_SUBS and True
				return
		service = self.session.nav.getCurrentService()
		info = service and service.info()
		config.subtitles.dvb_subtitles_centered.value = info and info.getInfo(iServiceInformation.sCenterDVBSubs) and True

	def __serviceChanged(self):
		if self.selected_subtitle:
			self.selected_subtitle = None
			self.subtitle_window.hide()

	def __updatedInfo(self):
		if not self.selected_subtitle:
			subtitle = self.getCurrentServiceSubtitle()
			cachedsubtitle = subtitle and subtitle.getCachedSubtitle()
			if cachedsubtitle:
				self.enableSubtitle(cachedsubtitle)
				self.doCenterDVBSubs()

	def enableSubtitle(self, selectedSubtitle):
		subtitle = self.getCurrentServiceSubtitle()
		self.selected_subtitle = selectedSubtitle
		if subtitle and self.selected_subtitle:
			subtitle.enableSubtitles(self.subtitle_window.instance, self.selected_subtitle)
			self.showSubtitles()
			self.doCenterDVBSubs()
		else:
			if subtitle:
				subtitle.disableSubtitles(self.subtitle_window.instance)
			self.subtitle_window.hide()

	def restartSubtitle(self):
		if self.selected_subtitle:
			self.enableSubtitle(self.selected_subtitle)

	def toggleSubtitleShown(self):
		config.subtitles.show.value = not config.subtitles.show.value
		self.VideoMode_window.setText(_("Subtitles enabled") if config.subtitles.show.value else _("Subtitles disabled"))
		self.showSubtitles()

	def showSubtitles(self):
		if config.subtitles.show.value:
			self.subtitle_window.show()
		else:
			self.subtitle_window.hide()


class InfoBarServiceErrorPopupSupport:
	def __init__(self):
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
				iPlayableService.evTuneFailed: self.__tuneFailed,
				iPlayableService.evTunedIn: self.__serviceStarted,
				iPlayableService.evStart: self.__serviceStarted
			})
		self.__serviceStarted()

	def __serviceStarted(self):
		self.closeNotificationInstantiateDialog()
		self.last_error = None
		RemovePopup(id="ZapError")

	def __tuneFailed(self):
		if not config.usage.hide_zap_errors.value or not config.usage.remote_fallback_enabled.value:
			service = self.session.nav.getCurrentService()
			info = service and service.info()
			error = info and info.getInfo(iServiceInformation.sDVBState)
			if not config.usage.remote_fallback_enabled.value and (error == eDVBServicePMTHandler.eventMisconfiguration or error == eDVBServicePMTHandler.eventNoResources):
				self.session.nav.currentlyPlayingServiceReference = None
				self.session.nav.currentlyPlayingServiceOrGroup = None

			if error == self.last_error:
				error = None
			else:
				self.last_error = error

			error = {
				eDVBServicePMTHandler.eventNoResources: _("No free tuner!"),
				eDVBServicePMTHandler.eventTuneFailed: _("Tune failed!"),
				eDVBServicePMTHandler.eventNoPAT: _("No data on transponder!\n(Timeout reading PAT)"),
				eDVBServicePMTHandler.eventNoPATEntry: _("Service not found!\n(SID not found in PAT)"),
				eDVBServicePMTHandler.eventNoPMT: _("Service invalid!\n(Timeout reading PMT)"),
				eDVBServicePMTHandler.eventNewProgramInfo: None,
				eDVBServicePMTHandler.eventTuned: None,
				eDVBServicePMTHandler.eventSOF: None,
				eDVBServicePMTHandler.eventEOF: None,
				eDVBServicePMTHandler.eventMisconfiguration: _("Service unavailable!\nCheck tuner configuration!"),
			}.get(error)  # This returns None when the key not exist in the dictionary.

			if error and not config.usage.hide_zap_errors.value:
				self.closeNotificationInstantiateDialog()
				if hasattr(self, "dishDialog") and not self.dishDialog.dishState():
					AddPopup(text=error, type=MessageBox.TYPE_ERROR, timeout=5, id="ZapError")


class InfoBarPowersaver:
	def __init__(self):
		self.inactivityTimer = eTimer()
		self.inactivityTimer.callback.append(self.inactivityTimeout)
		self.restartInactiveTimer()
		self.sleepTimer = eTimer()
		self.sleepStartTime = 0
		self.sleepTimer.callback.append(self.sleepTimerTimeout)
		eActionMap.getInstance().bindAction('', -maxsize - 1, self.keypress)

	def keypress(self, key, flag):
		if flag:
			self.restartInactiveTimer()

	def restartInactiveTimer(self):
		time = abs(int(config.usage.inactivity_timer.value))
		if time:
			self.inactivityTimer.startLongTimer(time)
		else:
			self.inactivityTimer.stop()

	def inactivityTimeout(self):
		if config.usage.inactivity_timer_blocktime.value:
			curtime = localtime(time())
			if curtime.tm_year > 1970:  # check if the current time is valid
				duration = blocktime = extra_time = False
				if config.usage.inactivity_timer_blocktime_by_weekdays.value:
					weekday = curtime.tm_wday
					if config.usage.inactivity_timer_blocktime_day[weekday].value:
						blocktime = True
						begintime = tuple(config.usage.inactivity_timer_blocktime_begin_day[weekday].value)
						endtime = tuple(config.usage.inactivity_timer_blocktime_end_day[weekday].value)
						extra_time = config.usage.inactivity_timer_blocktime_extra_day[weekday].value
						begintime_extra = tuple(config.usage.inactivity_timer_blocktime_extra_begin_day[weekday].value)
						endtime_extra = tuple(config.usage.inactivity_timer_blocktime_extra_end_day[weekday].value)
				else:
					blocktime = True
					begintime = tuple(config.usage.inactivity_timer_blocktime_begin.value)
					endtime = tuple(config.usage.inactivity_timer_blocktime_end.value)
					extra_time = config.usage.inactivity_timer_blocktime_extra.value
					begintime_extra = tuple(config.usage.inactivity_timer_blocktime_extra_begin.value)
					endtime_extra = tuple(config.usage.inactivity_timer_blocktime_extra_end.value)
				curtime = (curtime.tm_hour, curtime.tm_min, curtime.tm_sec)
				if blocktime and (begintime <= endtime and (curtime >= begintime and curtime < endtime) or begintime > endtime and (curtime >= begintime or curtime < endtime)):
					duration = (endtime[0] * 3600 + endtime[1] * 60) - (curtime[0] * 3600 + curtime[1] * 60 + curtime[2])
				elif extra_time and (begintime_extra <= endtime_extra and (curtime >= begintime_extra and curtime < endtime_extra) or begintime_extra > endtime_extra and (curtime >= begintime_extra or curtime < endtime_extra)):
					duration = (endtime_extra[0] * 3600 + endtime_extra[1] * 60) - (curtime[0] * 3600 + curtime[1] * 60 + curtime[2])
				if duration:
					if duration < 0:
						duration += 24 * 3600
					self.inactivityTimer.startLongTimer(duration)
					return
		if Screens.Standby.inStandby:
			self.inactivityTimeoutCallback(True)
		else:
			message = _("Your receiver will got to standby due to inactivity.") + "\n" + _("Do you want this?")
			self.session.openWithCallback(self.inactivityTimeoutCallback, MessageBox, message, timeout=60, simple=True, default=False, timeout_default=True)

	def inactivityTimeoutCallback(self, answer):
		if answer:
			self.goStandby()
		else:
			print("[InfoBarGenerics] Powersaver abort")

	def sleepTimerState(self):
		if self.sleepTimer.isActive():
			return (self.sleepStartTime - time()) / 60
		return 0

	def setSleepTimer(self, sleepTime):
		print("[InfoBarGenerics] Powersaver set sleeptimer", sleepTime)
		if sleepTime:
			m = abs(sleepTime / 60)
			message = _("The sleep timer has been activated.") + "\n" + _("And will put your receiver in standby over ") + ngettext("%d minute", "%d minutes", m) % m
			self.sleepTimer.startLongTimer(sleepTime)
			self.sleepStartTime = time() + sleepTime
		else:
			message = _("The sleep timer has been disabled.")
			self.sleepTimer.stop()
		AddPopup(message, type=MessageBox.TYPE_INFO, timeout=5)

	def sleepTimerTimeout(self):
		if not Screens.Standby.inStandby:
			list = [(_("No"), False), (_("Extend sleeptimer 15 minutes"), "extend"), (_("Yes"), True)]
			message = _("Your receiver will got to stand by due to the sleeptimer.")
			message += "\n" + _("Do you want this?")
			self.session.openWithCallback(self.sleepTimerTimeoutCallback, MessageBox, message, timeout=60, simple=True, list=list, timeout_default=True)

	def sleepTimerTimeoutCallback(self, answer):
		if answer == "extend":
			print("[InfoBarGenerics] Powersaver extend sleeptimer")
			self.setSleepTimer(900)
		elif answer:
			self.goStandby()
		else:
			print("[InfoBarGenerics] Powersaver abort")
			self.setSleepTimer(0)

	def goStandby(self):
		if not Screens.Standby.inStandby:
			print("[InfoBarGenerics] Powersaver goto standby")
			self.session.open(Screens.Standby.Standby)


class InfoBarHdmi:
	def __init__(self):
		self.hdmi_enabled = False
		self.hdmi_enabled_full = False
		self.hdmi_enabled_pip = False
		if BoxInfo.getItem("HDMIin"):
			if not self.hdmi_enabled_full:
				self.addExtension((self.getHDMIInFullScreen, self.HDMIInFull, lambda: True))
			if not self.hdmi_enabled_pip:
				self.addExtension((self.getHDMIInPiPScreen, self.HDMIInPiP, lambda: True))
		self["HDMIActions"] = HelpableActionMap(self, ["InfobarHDMIActions"], {
			"HDMIin": (self.HDMIIn, _("Switch to HDMI-IN mode")),
			"HDMIinLong": (self.HDMIInLong, _("Switch to HDMI-IN mode")),
		}, prio=2, description=_("HDMI-IN Actions"))

	def HDMIInLong(self):
		if not hasattr(self.session, "pip") and not self.session.pipshown:
			self.session.pip = self.session.instantiateDialog(PictureInPicture)
			self.session.pip.playService(hdmiInServiceRef())
			self.session.pip.show()
			self.session.pipshown = True
			self.session.pip.servicePath = self.servicelist.getCurrentServicePath()
		elif BoxInfo.getItem("HasHDMIinPiP"):
			curref = self.session.pip.getCurrentService()
			if curref and curref.type != eServiceReference.idServiceHDMIIn:
				self.session.pip.playService(hdmiInServiceRef())
				self.session.pip.servicePath = self.servicelist.getCurrentServicePath()
			else:
				self.session.pipshown = False
				del self.session.pip

	def HDMIIn(self):
		slist = self.servicelist
		curref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if curref and curref.type != eServiceReference.idServiceHDMIIn:
			self.session.nav.playService(hdmiInServiceRef())
		else:
			self.session.nav.playService(slist.servicelist.getCurrent())

	def getHDMIInFullScreen(self):
		if not self.hdmi_enabled_full:
			return _("Turn on HDMI-IN Full screen mode")
		else:
			return _("Turn off HDMI-IN Full screen mode")

	def getHDMIInPiPScreen(self):
		if not self.hdmi_enabled_pip:
			return _("Turn on HDMI-IN PiP mode")
		else:
			return _("Turn off HDMI-IN PiP mode")

	def HDMIInPiP(self):
		if not hasattr(self.session, "pip") and not self.session.pipshown:
			self.hdmi_enabled_pip = True
			self.session.pip = self.session.instantiateDialog(PictureInPicture)
			self.session.pip.playService(hdmiInServiceRef())
			self.session.pip.show()
			self.session.pipshown = True
			self.session.pip.servicePath = self.servicelist.getCurrentServicePath()
		else:
			curref = self.session.pip.getCurrentService()
			if curref and curref.type != eServiceReference.idServiceHDMIIn:
				self.hdmi_enabled_pip = True
				self.session.pip.playService(hdmiInServiceRef())
				self.session.pip.servicePath = self.servicelist.getCurrentServicePath()
			else:
				self.hdmi_enabled_pip = False
				self.session.pipshown = False
				del self.session.pip

	def HDMIInFull(self):
		slist = self.servicelist
		curref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if curref and curref.type != eServiceReference.idServiceHDMIIn:
			self.hdmi_enabled_full = True
			self.session.nav.playService(hdmiInServiceRef())
		else:
			self.hdmi_enabled_full = False
			self.session.nav.playService(slist.servicelist.getCurrent())


# ################################################################
# Handle BSOD (python crashes) and show information after crash. #
# ################################################################
#
class InfoBarHandleBsod:
	def __init__(self):
		self.bsodCount = 0
		self.bsodIsShown = False
		self.bsodLastWarning = False
		self.bsodTimer = eTimer()
		self.bsodTimer.callback.append(self.bsodTimeout)
		self.bsodTimer.start(1000, True)
		config.crash.bsodpython_ready.setValue(True)

	def bsodTimeout(self):
		def bsodTimeoutCallback(answer):
			if answer and self.bsodLastWarning:
				resetBsodCounter()
			self.bsodIsShown = False
			self.bsodLastWarning = False

		self.bsodTimer.start(1000, True)
		if not Screens.Standby.inStandby and not self.bsodIsShown:
			bsodOccurences = getBsodCounter()
			if config.crash.bsodpython.value and self.bsodCount < bsodOccurences:
				bsodMax = int(config.crash.bsodmax.value) or 100
				writeLog = bsodOccurences == 1 or not bsodOccurences > int(config.crash.bsodhide.value) or bsodOccurences >= bsodMax
				crashText = _("Your Receiver has a Software problem detected. Since the last reboot it has occurred %d times.\n") % bsodOccurences
				crashText += _("(Attention: There will be a restart after %d crashes.)") % bsodMax
				if writeLog:
					crashText += f"\n{"-" * 80}\n"
					crashText += _("A crash log was %s created in '%s'") % ((_("not"), "")[int(writeLog)], config.crash.debug_path.value)
				if bsodOccurences >= bsodMax:
					crashText += f"\n{"-" * 80}\n"
					crashText += _("Warning: This is the last crash before an automatic restart is performed.\n")
					crashText += _("Should the crash counter be reset to prevent a restart?")
					self.bsodLastWarning = True
				try:
					self.session.openWithCallback(bsodTimeoutCallback, MessageBox, crashText, type=MessageBox.TYPE_YESNO if self.bsodLastWarning else MessageBox.TYPE_ERROR, default=False, close_on_any_key=not self.bsodLastWarning, typeIcon=MessageBox.TYPE_ERROR)
					self.bsodIsShown = True
				except Exception as err:
					print(f"[InfoBarGenerics] InfoBarHandleBsod: Error '{str(err)}' displaying crash screen!")
					self.bsodTimer.stop()
					self.bsodTimer.start(5000, True)
					bsodTimeoutCallback(False)
					raise
			self.bsodCount = bsodOccurences
