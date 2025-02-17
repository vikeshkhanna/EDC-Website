# Create your views here.
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import render_to_response, get_object_or_404, get_list_or_404
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.template import RequestContext
from constants import *
from datetime import datetime
from events.models import *
from events.forms import *
from about.models import Member
from associates.models import Sponsor
from datetime import datetime, timedelta


#define constant
url = 'events/separate/'

def create_events_menu(event):
	events_menu = []
	items = event.menu_items.all()

	for item in items:
		events_menu.append(Item(item.name,item.url))
	return events_menu	

def get_event(domain,num):
	cat = get_object_or_404(EventCategory,domain=domain)
	if cat.flagship: # A flagship event
		if not num:
			latest = Event.objects.filter(category=cat.id).order_by('-date_event')

			if not latest:		#No entry in database
				raise Http404
			else:
				event = latest[0]		#Pick up the latest. 		
		else:
			event = get_object_or_404(Event,category = cat.id,date_event__year=num)
	
	else: #Not flagship
		event = get_object_or_404(Event,pk=num)
	return event

#Index
def index(request):
	count=0	
	today=datetime.now()
	event_categories = EventCategory.objects.all()
	event_categories_nf = EventCategory.objects.filter(flagship=False)
	events = list(Event.objects.filter(date_event__gt=datetime.now()-EVENT_RECENT_LIMIT,parent=None).order_by('-date_event'))

	for event in events:
		if not event.category.is_flagship():
			count = count+1;

	return render_to_response('events/index.html',{'name':'Events','list':menu,'event_categories':event_categories,'events':events,'count':count},context_instance=RequestContext(request))		


#Introduction
def introduction(request,domain,num=None):
	fb_link = None
	year = num
	separate = False
	cat = get_object_or_404(EventCategory,domain=domain)
	event = get_event(domain,num)

	if cat.flagship: # A flagship event
		separate = True
		if not year:
			return HttpResponseRedirect('%s%s'%(request.path,event.date_event.year))		
	else: #Not flagship
		if event.separate: #should be a separate website				
			separate = True

	sponsors = Sponsor.objects.filter(event = event, event_homepage_display=True)

	if event.category.fest:
		guests = []
		temp_list = []
		events = Event.objects.filter(parent=event)

		if events:
			for e in events:
				temp_list =  list(Guest.objects.filter(event=e))
				guests.extend(temp_list)
	else:
		guests = Guest.objects.filter(event=event)
	

	coordinators = event.coordinator.all()
	announcements = Announcement.objects.filter(event=event)	

	if separate:
		return render_to_response(url+'introduction.html',{'name':'Introduction','list':create_events_menu(event),'event':event,'sponsors':sponsors,'coordinators':coordinators,'guests':guests,'announcements':announcements},context_instance=RequestContext(request))
	else:
		return render_to_response('events/inline_event.html',{'name':'Events','list':menu,'title':event.name,'event':event,'sponsors':sponsors,'coordinators':coordinators,'guests':guests,'announcements': announcements},context_instance=RequestContext(request))
		
#Overview
def overview(request,domain,num):
	event = get_event(domain,num)
	return render_to_response(url +'overview.html',{'name':'Overview','list':create_events_menu(event),'event':event},context_instance=RequestContext(request))


#Timeline
def timeline(request,domain,num):
	event = get_event(domain,num)
	stages = Stage.objects.filter(event=event).order_by('date_start')
	return render_to_response(url +'timeline.html',{'name':'Timeline','list':create_events_menu(event),'event':event,'stages':stages},context_instance=RequestContext(request))


#Incentives
def incentives(request,domain,num):
	event = get_event(domain,num)
	incentives = Incentive.objects.filter(event=event)
	return render_to_response(url +'incentives.html',{'name':'Incentives','list':create_events_menu(event),'event':event,'incentives':incentives},context_instance=RequestContext(request))

#FAQ
def faq(request,domain,num):
	event = get_event(domain,num)
	faq = FAQ.objects.filter(event=event)
	coordinators = event.coordinator.all()

	return render_to_response(url +'faq.html',{'name':'FAQ','list':create_events_menu(event),'event':event,'faq':faq,'coordinators':coordinators},context_instance=RequestContext(request))

#Contacts
def contact(request,domain,num):
	event = get_event(domain,num)
	coordinators = event.coordinator.all()
	webmasters = Member.objects.filter(rank = ranks['web_master'])

	return render_to_response(url +'contact.html',{'name':'Contact','list':create_events_menu(event),'event':event,'faq':faq,'coordinators':coordinators,'webmasters':webmasters},context_instance=RequestContext(request))


#Sponsors
def sponsors(request,domain,num):
	event = get_event(domain,num)
	sponsors = Sponsor.objects.filter(event = event)

	return render_to_response(url +'sponsors.html',{'name':'Sponsors','list':create_events_menu(event),'event':event,'sponsors':sponsors},context_instance=RequestContext(request))


#Rules
def rules(request,domain,num):
	event = get_event(domain,num)

	return render_to_response(url +'rules.html',{'name':'Rules','list':create_events_menu(event),'event':event,'rules':event.rules},context_instance=RequestContext(request))


#Participation is off
def participation_off(request,event):
	state = None
	"""state definition 	
	   0: Participation not open yet
	   1: Participation closed.
	"""
	if not event.has_participation_started():
		state=0
	elif event.has_participation_ended():
		state=1	

	return render_to_response(url +'participate_dead.html',{'name':'Participate','list':create_events_menu(event),'event':event,'state':state},context_instance=RequestContext(request))


#Participate
def participate(request,domain,num):
	event = get_event(domain,num)
	form = TeamForm()
	state= int(request.GET.get('state',0)) 
	msg=None
	check1=None
	check2 = None
	team = None

	"""state definition 	
	   0: Participation open and leader not registered
	   1: Participation open and leader team not confirmed
	   2: Participation open and user registered with some other team
	"""

	#Late or Early logic lies 
	if not event.is_participation_valid():
		return participation_off(request,event)
	
	try:
		user = User.objects.get(email=request.session['session_id'])
	except KeyError:
		return HttpResponseRedirect('/users/login/?next=%s&restricted=True'%request.path)		

	try:
		team = Team.objects.get(leader=user,event=event)
	except ObjectDoesNotExist:
		pass
	
	try:
		check1=Team.objects.get(email1=user.email,event=event)
		check2=Team.objects.get(email2=user.email,event=event)
	except ObjectDoesNotExist:
		pass

	if check1 or check2:
		if check1:
			team = check1
		else:
			team = check2
		state=2
		return render_to_response(url +'team_registration.html',{'name':'Participate','list':create_events_menu(event),'user':user,'event':event,'state':state,'team':team},context_instance=RequestContext(request))	

	else:
		if team:
			if team.confirmed:
				return HttpResponseRedirect(request.path + 'submit/')		
	
		if request.method=='POST':
			form = TeamForm(request.POST)
			
			if form.is_valid():
				n = form.cleaned_data['name'] 
				m1 = form.cleaned_data['member1']
				e1 = form.cleaned_data['email1']
				o1 = form.cleaned_data['organisation1']
				m2 = form.cleaned_data['member2']
				e2 = form.cleaned_data['email2']
				o2 = form.cleaned_data['organisation2']
			
				if team:	
					team.name = n		
					team.member1=m1
					team.email1 =e1
					team.organisation1 = o1
					team.member2=m2
					team.email2=e2
					team.organisation2 = o2
				else:
					team = Team(leader=user,name=n,member1=m1,email1=e1,organisation1=o1,member2=m2,email2=e2,organisation2=o2,confirmed=False,event=event)
		
				team.save()
				msg="Please confirm your details. These cannot be edited later"
				state=1
			else:
				state=0
		else:
			if team:
				form = TeamForm({'member1':team.member1,'name':team.name,'email1':team.email1,'organisation1':team.organisation1,'member2':team.member2,'email2':team.email2,'organisation2':team.organisation2})
	
	return render_to_response(url +'team_registration.html',{'name':'Participate','list':create_events_menu(event),'user':user,'event':event,'state':state,'form':form,'msg':msg},context_instance=RequestContext(request))
	

#Confirm
def confirm(request,domain,num):
	event = get_event(domain,num)
	try:
		user = get_object_or_404(User,email=request.session['session_id'])
	except KeyError:
		return HttpResponseRedirect('/users/login/?next=%s'%request.path)		
		
	team = get_object_or_404(Team,event=event,leader=user)
	team.confirmed = True
	team.save()
	return HttpResponseRedirect('/events/' + event.get_link() + '/participate/submit/?state=confirmed')

#Handler
def submit(request,domain,num):
	event = get_event(domain,num)
	msg = None
	
	#Late or Early logic lies 
	if not event.is_participation_valid():
		return participation_off(request,event)

	try:
		user = User.objects.get(email=request.session['session_id'])
	except KeyError:
		return HttpResponseRedirect('/users/login/?next=%s&restricted=True'%request.path)		
	
	try:
		team = Team.objects.get(leader=user,event=event)
	except ObjectDoesNotExist:
		return HttpResponseRedirect('/events/' + event.get_link() + '/participate/')

	if not team:
		return HttpResponseRedirect('/events/%s/participate'%event.get_link())		

	if domain=='arth':
		arth_data = None
		try:
			arth_data = ArthData.objects.get(team=team,event=team.event)	
			form = ArthForm(instance=arth_data)
		except ObjectDoesNotExist:
			form = ArthForm()

		if request.method=='POST':
			form=ArthForm(request.POST)	
		
			if form.is_valid():
				if arth_data:
					form = ArthForm(request.POST,instance=arth_data)
				else:	
					arth_data = form.save(commit=False)
					arth_data.team = team	
					arth_data.event = event
					
				form.save()
					
				msg = 'Your changes were saved successfully.'

		return render_to_response(url +'participate_arth.html',{'name':'Participate','list':create_events_menu(event),'event':event,'form':form,'msg':msg,'team':team},context_instance=RequestContext(request))
	#return HttpResponse(domain=='arth')


#result
def result(request,domain,num):
	event = get_event(domain,num)

	if domain=='arth':
		if not event.is_result_declared():
			return render_to_response(url +'result_dead.html',{'name':'Results','list':create_events_menu(event),'event':event,},context_instance=RequestContext(request))

		teams = Team.objects.filter(event=event,selected=True)

		return render_to_response(url +'result_arth.html',{'name':'Results','list':create_events_menu(event),'event':event,'teams':teams},context_instance=RequestContext(request))
	

	raise Http404

def events(request,domain,num):
	event = get_event(domain,num)
	#events = Event.objects.filter(parent=event)
	categories = EventCategory.objects.all()
	guests = []
	valid_categories = []
	events = []
	upcoming = None
	upcoming_guests = None
	for cat in categories:
		event_list = list(Event.objects.filter(parent=event,category=cat).order_by('-date_event'))		
		
		if event_list:
			for x in event_list:
				guest_list = list(Guest.objects.filter(event=x))[0:3]
				if guest_list:
					guests.extend(guest_list)

			events.extend(event_list)
			valid_categories.append(cat)
		
	temp_upcoming = Event.objects.filter(parent=event).order_by('date_event')

	for x in temp_upcoming:
		if x.date_event > datetime.now():
			upcoming = x
			break
	
	if upcoming:
		upcoming_guests = Guest.objects.filter(event=upcoming)

	return render_to_response(url +'events.html',{'name':'Events','list':create_events_menu(event),'event':event,'events':events,'upcoming':upcoming,'upcoming_guests':upcoming_guests, 'categories':valid_categories,'guests':guests},context_instance=RequestContext(request))
	#return HttpResponse(event)

def inside_events(request,domain,num):
	event = get_event(domain,num)
	coordinators = event.coordinator.all()
	sponsors = Sponsor.objects.filter(event = event, event_homepage_display=True)
	
	return render_to_response(url +'inside_event.html',{'event':event,'coordinators':coordinators,'sponsors':sponsors}, context_instance=RequestContext(request))
	#return HttpResponse(event)

def schedule(request,domain,num):
	event = get_event(domain,num)
	
	return render_to_response(url +'schedule.html',{'name':'Schedule','list':create_events_menu(event),'event':event,},context_instance=RequestContext(request))


