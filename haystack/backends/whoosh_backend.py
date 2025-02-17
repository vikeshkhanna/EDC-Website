import os
import re
import shutil
import threading
import warnings
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models.loading import get_model
from django.utils.datetime_safe import datetime
from django.utils.encoding import force_unicode
from haystack.backends import BaseSearchBackend, BaseSearchQuery, log_query
from haystack.haystack_constants import ID, DJANGO_CT, DJANGO_ID
from haystack.fields import DateField, DateTimeField, IntegerField, FloatField, BooleanField, MultiValueField
from haystack.exceptions import MissingDependency, SearchBackendError
from haystack.models import SearchResult
from haystack.utils import get_identifier
try:
    set
except NameError:
    from sets import Set as set
try:
    import json
except ImportError:
    try:
        import simplejson as json
    except ImportError:
        from django.utils import simplejson as json

try:
    import whoosh
except ImportError:
    raise MissingDependency("The 'whoosh' backend requires the installation of 'Whoosh'. Please refer to the documentation.")

# Bubble up the correct error.
from whoosh.analysis import StemmingAnalyzer
from whoosh.fields import Schema, IDLIST, STORED, TEXT, KEYWORD, NUMERIC, BOOLEAN, DATETIME
from whoosh.fields import ID as WHOOSH_ID
from whoosh import index
from whoosh.qparser import QueryParser
from whoosh.filedb.filestore import FileStorage, RamStorage
from whoosh.searching import ResultsPage
from whoosh.spelling import SpellChecker
from whoosh.writing import AsyncWriter

# Handle minimum requirement.
if not hasattr(whoosh, '__version__') or whoosh.__version__ < (1, 1, 1):
    raise MissingDependency("The 'whoosh' backend requires version 1.1.1 or greater.")


DATETIME_REGEX = re.compile('^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(\.\d{3,6}Z?)?$')
BACKEND_NAME = 'whoosh'
LOCALS = threading.local()
LOCALS.RAM_STORE = None


class SearchBackend(BaseSearchBackend):
    # Word reserved by Whoosh for special use.
    RESERVED_WORDS = (
        'AND',
        'NOT',
        'OR',
        'TO',
    )
    
    # Characters reserved by Whoosh for special use.
    # The '\\' must come first, so as not to overwrite the other slash replacements.
    RESERVED_CHARACTERS = (
        '\\', '+', '-', '&&', '||', '!', '(', ')', '{', '}',
        '[', ']', '^', '"', '~', '*', '?', ':', '.',
    )
    
    def __init__(self, site=None):
        super(SearchBackend, self).__init__(site)
        self.setup_complete = False
        self.use_file_storage = True
        self.post_limit = getattr(settings, 'HAYSTACK_WHOOSH_POST_LIMIT', 128 * 1024 * 1024)
        
        if getattr(settings, 'HAYSTACK_WHOOSH_STORAGE', 'file') != 'file':
            self.use_file_storage = False
        
        if self.use_file_storage and not hasattr(settings, 'HAYSTACK_WHOOSH_PATH'):
            raise ImproperlyConfigured('You must specify a HAYSTACK_WHOOSH_PATH in your settings.')
    
    def setup(self):
        """
        Defers loading until needed.
        """
        new_index = False
        
        # Make sure the index is there.
        if self.use_file_storage and not os.path.exists(settings.HAYSTACK_WHOOSH_PATH):
            os.makedirs(settings.HAYSTACK_WHOOSH_PATH)
            new_index = True
        
        if self.use_file_storage and not os.access(settings.HAYSTACK_WHOOSH_PATH, os.W_OK):
            raise IOError("The path to your Whoosh index '%s' is not writable for the current user/group." % settings.HAYSTACK_WHOOSH_PATH)
        
        if self.use_file_storage:
            self.storage = FileStorage(settings.HAYSTACK_WHOOSH_PATH)
        else:
            global LOCALS
            
            if LOCALS.RAM_STORE is None:
                LOCALS.RAM_STORE = RamStorage()
            
            self.storage = LOCALS.RAM_STORE
        
        self.content_field_name, self.schema = self.build_schema(self.site.all_searchfields())
        self.parser = QueryParser(self.content_field_name, schema=self.schema)
        
        if new_index is True:
            self.index = self.storage.create_index(self.schema)
        else:
            try:
                self.index = self.storage.open_index(schema=self.schema)
            except index.EmptyIndexError:
                self.index = self.storage.create_index(self.schema)
        
        self.setup_complete = True
    
    def build_schema(self, fields):
        schema_fields = {
            ID: WHOOSH_ID(stored=True, unique=True),
            DJANGO_CT: WHOOSH_ID(stored=True),
            DJANGO_ID: WHOOSH_ID(stored=True),
        }
        # Grab the number of keys that are hard-coded into Haystack.
        # We'll use this to (possibly) fail slightly more gracefully later.
        initial_key_count = len(schema_fields)
        content_field_name = ''
        
        for field_name, field_class in fields.items():
            if field_class.is_multivalued:
                if field_class.indexed is False:
                    schema_fields[field_class.index_fieldname] = IDLIST(stored=True)
                else:
                    schema_fields[field_class.index_fieldname] = KEYWORD(stored=True, commas=True, scorable=True)
            elif field_class.field_type in ['date', 'datetime']:
                schema_fields[field_class.index_fieldname] = DATETIME(stored=field_class.stored)
            elif field_class.field_type == 'integer':
                schema_fields[field_class.index_fieldname] = NUMERIC(stored=field_class.stored, type=int)
            elif field_class.field_type == 'float':
                schema_fields[field_class.index_fieldname] = NUMERIC(stored=field_class.stored, type=float)
            elif field_class.field_type == 'boolean':
                schema_fields[field_class.index_fieldname] = BOOLEAN(stored=field_class.stored)
            else:
                schema_fields[field_class.index_fieldname] = TEXT(stored=True, analyzer=StemmingAnalyzer())
            
            if field_class.document is True:
                content_field_name = field_class.index_fieldname
        
        # Fail more gracefully than relying on the backend to die if no fields
        # are found.
        if len(schema_fields) <= initial_key_count:
            raise SearchBackendError("No fields were found in any search_indexes. Please correct this before attempting to search.")
        
        return (content_field_name, Schema(**schema_fields))
    
    def update(self, index, iterable, commit=True):
        if not self.setup_complete:
            self.setup()
        
        self.index = self.index.refresh()
        writer = AsyncWriter(self.index)
        
        for obj in iterable:
            doc = index.full_prepare(obj)
            
            # Really make sure it's unicode, because Whoosh won't have it any
            # other way.
            for key in doc:
                doc[key] = self._from_python(doc[key])
            
            writer.update_document(**doc)
        
        if len(iterable) > 0:
            # For now, commit no matter what, as we run into locking issues otherwise.
            writer.commit()
            
            # If spelling support is desired, add to the dictionary.
            if getattr(settings, 'HAYSTACK_INCLUDE_SPELLING', False) is True:
                sp = SpellChecker(self.storage)
                sp.add_field(self.index, self.content_field_name)
    
    def remove(self, obj_or_string, commit=True):
        if not self.setup_complete:
            self.setup()
        
        self.index = self.index.refresh()
        whoosh_id = get_identifier(obj_or_string)
        self.index.delete_by_query(q=self.parser.parse(u'%s:"%s"' % (ID, whoosh_id)))
    
    def clear(self, models=[], commit=True):
        if not self.setup_complete:
            self.setup()
        
        self.index = self.index.refresh()
        
        if not models:
            self.delete_index()
        else:
            models_to_delete = []
            
            for model in models:
                models_to_delete.append(u"%s:%s.%s" % (DJANGO_CT, model._meta.app_label, model._meta.module_name))
            
            self.index.delete_by_query(q=self.parser.parse(u" OR ".join(models_to_delete)))
    
    def delete_index(self):
        # Per the Whoosh mailing list, if wiping out everything from the index,
        # it's much more efficient to simply delete the index files.
        if self.use_file_storage and os.path.exists(settings.HAYSTACK_WHOOSH_PATH):
            shutil.rmtree(settings.HAYSTACK_WHOOSH_PATH)
        elif not self.use_file_storage:
            self.storage.clean()
        
        # Recreate everything.
        self.setup()
        
    def optimize(self):
        if not self.setup_complete:
            self.setup()
        
        self.index = self.index.refresh()
        self.index.optimize()
    
    @log_query
    def search(self, query_string, sort_by=None, start_offset=0, end_offset=None,
               fields='', highlight=False, facets=None, date_facets=None, query_facets=None,
               narrow_queries=None, spelling_query=None,
               limit_to_registered_models=None, **kwargs):
        if not self.setup_complete:
            self.setup()
        
        # A zero length query should return no results.
        if len(query_string) == 0:
            return {
                'results': [],
                'hits': 0,
            }
        
	query_string = force_unicode(query_string)
        
        # A one-character query (non-wildcard) gets nabbed by a stopwords
        # filter and should yield zero results.
        if len(query_string) <= 1 and query_string != u'*':
            return {
                'results': [],
                'hits': 0,
            }
        
        reverse = False
        
        if sort_by is not None:
            # Determine if we need to reverse the results and if Whoosh can
            # handle what it's being asked to sort by. Reversing is an
            # all-or-nothing action, unfortunately.
            sort_by_list = []
            reverse_counter = 0
            
            for order_by in sort_by:
                if order_by.startswith('-'):
                    reverse_counter += 1
            
            if len(sort_by) > 1 and reverse_counter > 1:
                raise SearchBackendError("Whoosh does not handle more than one field and any field being ordered in reverse.")
            
            for order_by in sort_by:
                if order_by.startswith('-'):
                    sort_by_list.append(order_by[1:])
                    
                    if len(sort_by_list) == 1:
                        reverse = True
                else:
                    sort_by_list.append(order_by)
                    
                    if len(sort_by_list) == 1:
                        reverse = False
                
            sort_by = sort_by_list[0]
        
        if facets is not None:
            warnings.warn("Whoosh does not handle faceting.", Warning, stacklevel=2)
        
        if date_facets is not None:
            warnings.warn("Whoosh does not handle date faceting.", Warning, stacklevel=2)
        
        if query_facets is not None:
            warnings.warn("Whoosh does not handle query faceting.", Warning, stacklevel=2)
        
        narrowed_results = None
        self.index = self.index.refresh()
        
        if limit_to_registered_models is None:
            limit_to_registered_models = getattr(settings, 'HAYSTACK_LIMIT_TO_REGISTERED_MODELS', True)
        
        if limit_to_registered_models:
            # Using narrow queries, limit the results to only models registered
            # with the current site.
            if narrow_queries is None:
                narrow_queries = set()
            
            registered_models = self.build_registered_models_list()
            
            if len(registered_models) > 0:
                narrow_queries.add('%s:(%s)' % (DJANGO_CT, ' OR '.join(registered_models)))
        
        if narrow_queries is not None:
            # Potentially expensive? I don't see another way to do it in Whoosh...
            narrow_searcher = self.index.searcher()
            
            for nq in narrow_queries:
                recent_narrowed_results = narrow_searcher.search(self.parser.parse(force_unicode(nq)))
             
                if narrowed_results:
                    narrowed_results.filter(recent_narrowed_results)
                else:
                   narrowed_results = recent_narrowed_results
        
        self.index = self.index.refresh()
        #Index is correct!
     
        if self.index.doc_count():
            searcher = self.index.searcher()
            parsed_query = self.parser.parse(query_string)
	    #Term('text',u'pink')            

            # In the event of an invalid/stopworded query, recover gracefully.
            if parsed_query is None:
                return {
                    'results': [],
                    'hits': 0,
                }
            
            # Prevent against Whoosh throwing an error. Requires an end_offset
            # greater than 0.
            if not end_offset is None and end_offset <= 0:
                end_offset = 1
            
            raw_results = searcher.search(parsed_query, limit=end_offset, sortedby=sort_by, reverse=reverse)
	           
	    # Handle the case where the results have been narrowed.
	    print narrowed_results

	    #User edit. Uncomment for original 		            
	    if narrowed_results:
                raw_results.filter(narrowed_results)
	    
            # Determine the page.
            page_num = 0
            
            if end_offset is None:
                end_offset = 1000000
            
            if start_offset is None:
                start_offset = 0
            
            page_length = end_offset - start_offset
            
            if page_length and page_length > 0:
                page_num = start_offset / page_length
            
            # Increment because Whoosh uses 1-based page numbers.
            page_num += 1
            
       
	    try:
                raw_page = ResultsPage(raw_results, page_num, page_length)
            except ValueError:
                return {
                    'results': [],
                    'hits': 0,
                    'spelling_suggestion': None,
                }

            return self._process_results(raw_page, highlight=highlight, query_string=query_string, spelling_query=spelling_query)
        
	else:
            if getattr(settings, 'HAYSTACK_INCLUDE_SPELLING', False):
                if spelling_query:
                    spelling_suggestion = self.create_spelling_suggestion(spelling_query)
                else:
                    spelling_suggestion = self.create_spelling_suggestion(query_string)
            else:
                spelling_suggestion = None
            
            return {
                'results': [],
                'hits': 0,
                'spelling_suggestion': spelling_suggestion,
            }
    
    def more_like_this(self, model_instance, additional_query_string=None,
                       start_offset=0, end_offset=None,
                       limit_to_registered_models=None, **kwargs):
        warnings.warn("Whoosh does not handle More Like This.", Warning, stacklevel=2)
        return {
            'results': [],
            'hits': 0,
        }
    
    def _process_results(self, raw_page, highlight=False, query_string='', spelling_query=None):
        from haystack import site
        results = []
        
        # It's important to grab the hits first before slicing. Otherwise, this
        # can cause pagination failures.
        hits = len(raw_page)
        
        facets = {}
        spelling_suggestion = None
        indexed_models = site.get_indexed_models()
        
        for doc_offset, raw_result in enumerate(raw_page):
            score = raw_page.score(doc_offset) or 0
            app_label, model_name = raw_result[DJANGO_CT].split('.')
            additional_fields = {}
            model = get_model(app_label, model_name)
            
            if model and model in indexed_models:
                for key, value in raw_result.items():
                    index = site.get_index(model)
                    string_key = str(key)
                    
                    if string_key in index.fields and hasattr(index.fields[string_key], 'convert'):
                        # Special-cased due to the nature of KEYWORD fields.
                        if isinstance(index.fields[string_key], MultiValueField):
                            if value is None or len(value) is 0:
                                additional_fields[string_key] = []
                            else:
                                additional_fields[string_key] = value.split(',')
                        else:
                            additional_fields[string_key] = index.fields[string_key].convert(value)
                    else:
                        additional_fields[string_key] = self._to_python(value)
                
                del(additional_fields[DJANGO_CT])
                del(additional_fields[DJANGO_ID])
                
                if highlight:
                    from whoosh import analysis
                    from whoosh.highlight import highlight, ContextFragmenter, UppercaseFormatter
                    sa = analysis.StemmingAnalyzer()
                    terms = [term.replace('*', '') for term in query_string.split()]
                    
                    additional_fields['highlighted'] = {
                        self.content_field_name: [highlight(additional_fields.get(self.content_field_name), terms, sa, ContextFragmenter(terms), UppercaseFormatter())],
                    }
                
                result = SearchResult(app_label, model_name, raw_result[DJANGO_ID], score, **additional_fields)
                results.append(result)
            else:
                hits -= 1
        
        if getattr(settings, 'HAYSTACK_INCLUDE_SPELLING', False):
            if spelling_query:
                spelling_suggestion = self.create_spelling_suggestion(spelling_query)
            else:
                spelling_suggestion = self.create_spelling_suggestion(query_string)
        
	
        return {
            'results': results,
            'hits': hits,
            'facets': facets,
            'spelling_suggestion': spelling_suggestion,
        }
    
    def create_spelling_suggestion(self, query_string):
        spelling_suggestion = None
        sp = SpellChecker(self.storage)
        cleaned_query = force_unicode(query_string)
        
        if not query_string:
            return spelling_suggestion
        
        # Clean the string.
        for rev_word in self.RESERVED_WORDS:
            cleaned_query = cleaned_query.replace(rev_word, '')
        
        for rev_char in self.RESERVED_CHARACTERS:
            cleaned_query = cleaned_query.replace(rev_char, '')
        
        # Break it down.
        query_words = cleaned_query.split()
        suggested_words = []
        
        for word in query_words:
            suggestions = sp.suggest(word, number=1)
            
            if len(suggestions) > 0:
                suggested_words.append(suggestions[0])
        
        spelling_suggestion = ' '.join(suggested_words)
        return spelling_suggestion
    
    def _from_python(self, value):
        """
        Converts Python values to a string for Whoosh.
        
        Code courtesy of pysolr.
        """
        if hasattr(value, 'strftime'):
            if not hasattr(value, 'hour'):
                value = datetime(value.year, value.month, value.day, 0, 0, 0)
        elif isinstance(value, bool):
            if value:
                value = True
            else:
                value = False
        elif isinstance(value, (list, tuple)):
            value = u','.join([force_unicode(v) for v in value])
        elif isinstance(value, (int, long, float)):
            # Leave it alone.
            pass
        else:
            value = force_unicode(value)
        return value
    
    def _to_python(self, value):
        """
        Converts values from Whoosh to native Python values.
        
        A port of the same method in pysolr, as they deal with data the same way.
        """
        if value == 'true':
            return True
        elif value == 'false':
            return False
        
        if value and isinstance(value, basestring):
            possible_datetime = DATETIME_REGEX.search(value)
            
            if possible_datetime:
                date_values = possible_datetime.groupdict()
            
                for dk, dv in date_values.items():
                    date_values[dk] = int(dv)
            
                return datetime(date_values['year'], date_values['month'], date_values['day'], date_values['hour'], date_values['minute'], date_values['second'])
        
        try:
            # Attempt to use json to load the values.
            converted_value = json.loads(value)
            
            # Try to handle most built-in types.
            if isinstance(converted_value, (list, tuple, set, dict, int, float, long, complex)):
                return converted_value
        except:
            # If it fails (SyntaxError or its ilk) or we don't trust it,
            # continue on.
            pass
        
        return value


class SearchQuery(BaseSearchQuery):
    def __init__(self, site=None, backend=None):
        super(SearchQuery, self).__init__(backend=backend)
        
        if backend is not None:
            self.backend = backend
        else:
            self.backend = SearchBackend(site=site)
    
    def _convert_datetime(self, date):
        if hasattr(date, 'hour'):
            return force_unicode(date.strftime('%Y%m%dT%H%M%S'))
        else:
            return force_unicode(date.strftime('%Y%m%dT000000'))
    
    def clean(self, query_fragment):
        """
        Provides a mechanism for sanitizing user input before presenting the
        value to the backend.
        
        Whoosh 1.X differs here in that you can no longer use a backslash
        to escape reserved characters. Instead, the whole word should be
        quoted.
        """
        words = query_fragment.split()
        cleaned_words = []
        
        for word in words:
            if word in self.backend.RESERVED_WORDS:
                word = word.replace(word, word.lower())
            
            for char in self.backend.RESERVED_CHARACTERS:
                if char in word:
                    word = "'%s'" % word
                    break
            
            cleaned_words.append(word)
        
        return ' '.join(cleaned_words)
    
    def build_query_fragment(self, field, filter_type, value):
        result = ''
        is_datetime = False
        
        if hasattr(value, 'strftime'):
            is_datetime = True
        
        if not filter_type in ('in', 'range'):
            # 'in' is a bit of a special case, as we don't want to
            # convert a valid list/tuple to string. Defer handling it
            # until later...
            value = self.backend._from_python(value)
        
        # Check to see if it's a phrase for an exact match.
        if isinstance(value, basestring) and ' ' in value:
            value = '"%s"' % value
        
        index_fieldname = self.backend.site.get_index_fieldname(field)
        
        # 'content' is a special reserved word, much like 'pk' in
        # Django's ORM layer. It indicates 'no special field'.
        if field == 'content':
            result = value
        else:
            filter_types = {
                'exact': "%s:%s",
                'gt': "%s:{%s TO}",
                'gte': "%s:[%s TO]",
                'lt': "%s:{TO %s}",
                'lte': "%s:[TO %s]",
                'startswith': "%s:%s*",
            }
            
            if filter_type == 'in':
                in_options = []
                
                for possible_value in value:
                    is_datetime = False
                    
                    if hasattr(possible_value, 'strftime'):
                        is_datetime = True
                    
                    pv = self.backend._from_python(possible_value)
                    
                    if is_datetime is True:
                        pv = self._convert_datetime(pv)
                    
                    in_options.append('%s:"%s"' % (index_fieldname, pv))
                
                result = "(%s)" % " OR ".join(in_options)
            elif filter_type == 'range':
                start = self.backend._from_python(value[0])
                end = self.backend._from_python(value[1])
                
                if hasattr(value[0], 'strftime'):
                    start = self._convert_datetime(start)
                
                if hasattr(value[1], 'strftime'):
                    end = self._convert_datetime(end)
                
                return "%s:[%s TO %s]" % (index_fieldname, start, end)
            else:
                if is_datetime is True:
                    value = self._convert_datetime(value)
                
                result = filter_types[filter_type] % (index_fieldname, value)
        
        return result
