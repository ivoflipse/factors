from __future__ import print_function

import numpy as np
import pandas as pd

from collections import OrderedDict
from settings import UPAGE, LOWAGE, MAXAGE, XLSWB, INSURANCE_IDS, MALE, FEMALE
from utils import dictify, prae_to_continuous, merge_two_dicts, cartesian, expand, x_to_series


class LifeTable(object):
    def __init__(self, tablename, xlswb=XLSWB):
        self.tablename = tablename
        self.xlswb = xlswb
        self.legend = self.get_legend()
        self.params = self.get_parameters()
        self.lx = self.get_lx()
        self.hx = self.get_hx()
        self.adjust = self.get_adjustments()
        self.ukv = self.get_ukv()
        self.testdata = self.get_test_data()
        self.pension_age = None
        self.intrest = None
        self.lookup = None
        self.cfs = None
        self.factors = None
        self.yield_curve = None

    def get_legend(self):
        df = pd.read_excel(self.xlswb, sheetname='tbl_insurance_types')
        df.set_index('id_type', inplace=True)
        return df.ix[INSURANCE_IDS]

    def get_parameters(self):
        df = pd.read_excel(self.xlswb, sheetname='tbl_tariff')
        df.set_index('name', inplace=True)
        return df.ix[self.tablename].to_dict()

    def get_lx(self):
        df = pd.read_excel(self.xlswb, sheetname='tbl_lx')
        df.set_index(['id', 'gender', 'age'], inplace=True)
        select = int(self.params['lx'])
        return {gender: df.ix[select].ix[gender] for gender in (MALE, FEMALE)}

    def get_hx(self):
        df = pd.read_excel(self.xlswb, sheetname='tbl_hx')
        df.set_index(['id', 'gender', 'age'], inplace=True)
        select = int(self.params['hx'])
        return {gender: df.ix[select].ix[gender] for gender in (MALE, FEMALE)}

    def get_adjustments(self):
        df = pd.read_excel(self.xlswb, sheetname='tbl_adjustments')
        select = self.params['adjustments']
        df = df[df['id'] == select]
        df.drop('id', axis=1, inplace=True)
        return dictify(df)

    def get_ukv(self):
        df = pd.read_excel(self.xlswb, sheetname='tbl_ukv')
        try:
            df = df[df['id'] == int(self.params['ukv'])]
        except ValueError:
            return None
        df.drop('id', axis=1, inplace=True)
        df.set_index(['gender', 'pension_age', 'intrest'], inplace=True)
        return df

    def get_test_data(self):
        df1 = pd.read_excel(self.xlswb, sheetname='tbl_testdata_values')
        df2 = pd.read_excel(self.xlswb, sheetname='tbl_testdata')
        out = pd.merge(df1, df2, left_on='testdata_id', right_on='id')
        return out[out['table'] == self.tablename]

    def npx(self, age, sex, nyears):
        """Returns probability person with given age is still alive after n years.

        Parameters:
        -----------
        age: int
        sex: either 'M' of 'F'
        nyears: int
        """
        future_age = np.minimum(age + nyears, MAXAGE)
        current_age = np.minimum(age, MAXAGE)
        return (float(self.lx[sex].ix[future_age]['lx']) /
                self.lx[sex].ix[current_age]['lx'])

    def qx(self, age, sex):
        """Returns the probability that person with given age will die within 1 year.

        Parameters:
        -----------
        age: int
        sex: either 'M' of 'F'
        """
        return 1 - self.npx(age, sex, 1)

    def nqx(self, age, sex, nyears):
        """Returns probability that person with will die
           in interval (nyears - 1, nyears).

        Parameters:
        -----------
        age: int
        sex: either 'M' of 'F'
        nyears: int
        """
        return self.npx(age, sex, nyears - 1) - self.npx(age, sex, nyears)

    def cf_annuity(self, age, lx, defer=0):
        """ Returns expected payments for (deferred) lifetime annuity.

        Parameters:
        -----------
        age: int
        lx: series
        defer: int
        """
        nrows = len(lx)
        assert nrows > defer, "Error: deferral period exceeds number of table rows."
        payments = pd.Series(defer * [0] + (nrows - defer) * [1])
        out = (payments * lx.shift(-age) / lx.ix[age]).fillna(0)
        out.index.rename('year', inplace=True)
        return out

    def cf_ay_avg(self, age_insured, sex_insured, pension_age=None, **kwargs):
        """ Returns cash flows non-defered annuity for beneficiary.

        Parameters:
        ----------
        age_insured: int
        sex_insured: either 'M' of 'F'

        insurance_type: either 'partner' or 'risk. Default 'partner'
        """
        insurance_type = kwargs.get('insurance_type', 'partner')
        assert sex_insured in (MALE, FEMALE), "sex insured should be either M of F!"
        sex_beneficiary = FEMALE if sex_insured == MALE else MALE
        delta = int(self.params['delta'])
        sign = 1 if sex_insured == MALE else -1
        gamma3 = self.adjust[sex_beneficiary][insurance_type]['CX3']
        tbl_beneficiary = (self.lx[FEMALE]['lx'] if sex_insured == MALE
                           else self.lx[MALE]['lx'])
        cf_ay_avg = (self.cf_annuity(age_insured - sign * delta + gamma3,
                     tbl_beneficiary) + self.cf_annuity(age_insured +
                     1 - sign * delta + gamma3, tbl_beneficiary)) / 2.
        cf_ay_avg = prae_to_continuous(cf_ay_avg)
        return {'payments': cf_ay_avg}

    def ay_avg(self, age_insured, sex_insured,
               intrest, insurance_type='partner'):
        """ Returns single premium non-defered annuity for beneficiary.

        Parameters:
        ----------
        age_insured: int
        sex_insured: either 'M' of 'F'
        intrest: int, float or Series

        insurance_type: either 'partner' or 'risk'. Default 'partner'.
        """
        cf_ay_avg = self.cf_ay_avg(age_insured, sex_insured, insurance_type)
        return self.pv({'insurance_id': 'OPLL',
                       'payments': cf_ay_avg['payments']}, intrest)

    def create_lookup_table(self, intrest):
        """ Returns a lookup table with age/sex dependent items for undefined partner.

        Parameters:
        -----------
        intrest: int, float of Series.
        """

        s = pd.DataFrame({'gender': (UPAGE - LOWAGE) * [MALE] +
                         (UPAGE - LOWAGE) * [FEMALE],
                         'age': range(LOWAGE, UPAGE) + range(LOWAGE, UPAGE)
                          })
        s['ay_avg'] = s.apply(lambda row: self.ay_avg(row['age'],
                              row['gender'], intrest), axis=1)
        s['hx_avg'] = s.apply(lambda row: (self.hx[row['gender']].ix[row['age']].values[0] +
                                           self.hx[row['gender']].ix[row['age'] + 1].values[0]) / 2., axis=1)
        s['alpha1'] = s.apply(lambda row: self.adjust[row['gender']]['partner']['CX1'], axis=1)
        s['factor'] = s.apply(lambda row: self.adjust[row['gender']]['partner']['fnett'] *
                              self.adjust[row['gender']]['partner']['fcorr'] *
                              self.adjust[row['gender']]['partner']['fOTS'],
                              axis=1)
        s['cf'] = s['ay_avg'] * s['hx_avg'] * s['factor']
        s.set_index(['gender', 'age'], inplace=True)
        return s

    def cf_retirement_pension(self, age_insured, sex_insured,
                              pension_age, **kwargs):
        """ Returns expected payments retirement pension.

        Parameters:
        -----------
        age_insured: int
        sex_insured: either 'M' of 'F'
        pension_age: int

        postnumerando: boolean
        """
        postnumerando = (kwargs['postnumerando'] if
                         'postnumerando' in kwargs else False)
        tbl_insured = self.lx[sex_insured]['lx']
        alpha1 = self.adjust[sex_insured]['retire']['CX1']
        alpha2 = self.adjust[sex_insured]['retire']['CX2']
        fnett, fcorr, fOTS = (self.adjust[sex_insured]['retire'][item]
                              for item in ['fnett', 'fcorr', 'fOTS'])
        cf = self.cf_annuity(age_insured + alpha2, tbl_insured,
                             defer=pension_age - age_insured + postnumerando)
        cf = cf * self.npx(age_insured + alpha1, sex_insured,
                           pension_age - age_insured)
        cf = cf / self.npx(age_insured + alpha2, sex_insured,
                           pension_age - age_insured)
        cf = prae_to_continuous(cf)
        return {'payments': cf * fnett * fcorr * fOTS}

    def cf_defined_partner(self, age_insured, sex_insured,
                           pension_age, **kwargs):
        """ Returns expected payments partner pension (defined partner).

        Parameters:
        ----------
        age_insured: int
        sex_insured: either 'M' of 'F'
        pension_age: int
        """
        assert sex_insured in (MALE, FEMALE), "sex insured should be either M of F!"
        sex_beneficiary = FEMALE if sex_insured == MALE else MALE
        tbl_insured = self.lx[sex_insured]['lx']
        tbl_beneficiary = self.lx[sex_beneficiary]['lx']
        delta = int(self.params['delta'])
        fnett, fcorr, fOTS = (self.adjust[sex_insured]['partner'][item]
                              for item in ['fnett', 'fcorr', 'fOTS'])
        alpha1 = self.adjust[sex_insured]['partner']['CX1']
        alpha2 = self.adjust[sex_insured]['partner']['CX2']
        gamma3 = self.adjust[sex_beneficiary]['partner']['CX3']
        sign = 1 if sex_insured == MALE else -1
        ay = self.cf_annuity(age_insured - sign * delta + gamma3,
                             tbl_beneficiary)
        ax = self.cf_annuity(age_insured + alpha1, tbl_insured)
        axy = ax.multiply(ay)
        f1 = self.cf_annuity(age_insured + alpha1, tbl_insured,
                             pension_age - age_insured)
        f1 = f1 * self.cf_annuity(age_insured - sign * delta + gamma3,
                                  tbl_beneficiary, pension_age - age_insured)
        f2 = self.cf_annuity(age_insured + alpha2,
                             tbl_insured, pension_age - age_insured)
        f2 = f2 * self.cf_annuity(age_insured - sign * delta + gamma3,
                                  tbl_beneficiary, pension_age - age_insured)
        temp1 = ((float(tbl_insured.ix[pension_age + alpha1]) /
                 tbl_insured.ix[age_insured + alpha1]))
        temp2 = ((float(tbl_insured.ix[age_insured + alpha2]) /
                 tbl_insured.ix[pension_age + alpha2]))
        f2 = f2 * temp1 * temp2
        out = fnett * fcorr * fOTS * (ay - axy + (f1 - f2))
        return {'payments': out}

    def cf_undefined_partner(self, age_insured, sex_insured,
                             pension_age, **kwargs):
        """ Returns expected payments partner pension (undefined partner).

        TO DO: intrest or yield curve really required to get cash flows
        undefined partner?

        Parameters:
        ----------
        age_insured: int
        sex_insured: either 'M' of 'F'
        pension_age: int

        intrest: float, series or list. Default = 3 pct!
        hx_pd: either 'None' for non-exchangable, 'one' for exchangable
        or 'ukv' for Aegon methodology (depreciated).

        """
        assert sex_insured in (MALE, FEMALE), "sex insured should be either M of F!"

        intrest = kwargs.get('intrest', None)
        if (intrest is None):
            msg1 = "Undefined partner cashflows require intrest"
            msg2 = "-- defaults intrest = 3pct"
            print("{0} {1}".format(msg1, msg2))
            intrest = 3  # default = 3 pct intrest rate!

        hx_pd = kwargs.get('hx_pd', None)
        # by default, undefined partner pension is assumed to be exchangable
        if (hx_pd is None) or (hx_pd == 'one'):
            hx_at_pensionage = 1
        elif hx_pd == 'ukv':
            try:
                hx_at_pensionage = self.ukv.ix[(sex_insured, pension_age,
                                                kwargs['intrest'])].values[0]
            except:
                print('Undefined partner cashflows require UKV -- defaults hx_pd = 1')
                hx_at_pensionage = 1
        else:
            hx_at_pensionage = self.hx[sex_insured]['hx'].ix[pension_age]

        fnett, fcorr, fOTS = (self.adjust[sex_insured]['partner'][item]
                              for item in ['fnett', 'fcorr', 'fOTS'])

        # cf till retirement
        if intrest == self.intrest:
            lookup = self.lookup
        else:
            lookup = self.create_lookup_table(intrest)
            self.lookup = lookup
            self.intrest = intrest

        cf_till_pension_age = (lookup.ix[sex_insured].
                               ix[age_insured:pension_age - 1])
        current_age = age_insured  # we need [k]q[current_age]
        cf_till_pension_age['age'] = cf_till_pension_age.index
        nq_current_age = cf_till_pension_age.apply(lambda row:
                                                   self.nqx(current_age + row['alpha1'],
                                                            sex_insured,
                                                            row['age'] - current_age + 1),
                                                   axis=1)
        cf_till_pension_age = cf_till_pension_age['cf'] * nq_current_age
        cf_till_pension_age = pd.DataFrame(cf_till_pension_age, columns=['cf'])

        # cf after retirement
        prob = self.npx(age_insured + lookup['alpha1'].loc[(sex_insured, age_insured)], sex_insured, pension_age - age_insured)
        cf_defined_partner = self.cf_defined_partner(pension_age, sex_insured, pension_age)
        cf_after_pension_age = hx_at_pensionage * prob * cf_defined_partner['payments']
        cf_after_pension_age = pd.DataFrame(cf_after_pension_age, columns=['cf'])
        cf_after_pension_age['age'] = cf_after_pension_age.index + pension_age
        cf_after_pension_age.set_index('age', inplace=True)
        cf = cf_till_pension_age.append(cf_after_pension_age)
        cf = pd.DataFrame(cf)
        cf['year'] = range(len(cf))
        cf.set_index('year', inplace=True)
        cf = pd.Series(cf['cf'])
        return {'age': age_insured, 'pension_age': pension_age, 'payments': cf}

    def cf_defined_one_year_risk(self, age_insured, sex_insured, pension_age, **kwargs):
        """ Ruturns expected cashflows one year risk premium (defined partner).

        ----------
        Parameters:
        ----------
        age_insured: int
        sex_insured: either 'M' of 'F'
        pension_age: int
        """
        # alpha1 = self.adjust[sex_insured]['risk']['CX1']
        # fnett, fcorr, fOTS = (self.adjust[sex_insured]['risk'][item]
        #                      for item in ['fnett', 'fcorr', 'fOTS'])
        #  --- risk premiums are considered to be part of partnerpension, so its adjustments are used! ---
        alpha1 = self.adjust[sex_insured]['partner']['CX1']
        fnett, fcorr, fOTS = (self.adjust[sex_insured]['partner'][item]
                              for item in ['fnett', 'fcorr', 'fOTS'])
        assert sex_insured in (MALE, FEMALE), "sex insured should be either M of F!"

        cf = self.cf_ay_avg(age_insured, sex_insured, insurance_type='partner')
        qx = self.qx(age_insured + alpha1, sex_insured)
        cf = cf['payments'] * qx * fnett * fcorr * fOTS
        return {'insurance_id': 'NPTL-B', 'payments': cf}

    def cf_undefined_one_year_risk(self, age_insured, sex_insured, pension_age, **kwargs):
        """ Ruturns expected cashflows one year risk premium (undefined partner).

        ----------
        Parameters:
        ----------
        age_insured: int
        sex_insured: either 'M' of 'F'
        pension_age: int
        """

        hx_avg = (self.hx[sex_insured].ix[age_insured].values[0] + self.hx[sex_insured].ix[age_insured + 1].values[0]) / 2.
        cf_defined_one_year_risk = self.cf_defined_one_year_risk(age_insured, sex_insured, pension_age, **kwargs)
        cf = hx_avg * cf_defined_one_year_risk['payments']
        return {'insurance_id': 'NPTL-O', 'payments': cf}

    def cf(self, insurance_id, age_insured, sex_insured, pension_age, **kwargs):
        """ Returns cash flows for given insurance type.

        Parameters:
        -----------
        insurance_id: either 'OPLL', 'NPLL-B', 'NPLL-O', 'NPLLRS', 'NPLLRU', 'NPTL-B' or 'NPTL-O'
        age_insured: int
        sex_insured: either 'M' of 'F'
        pension_age: int

        intrest: int, float or Series. Optional. Default 3pct.
        """

        switcher = {'OPLL': {'call': self.cf_retirement_pension, 'hx_pd': None},
                    'NPLL-B': {'call': self.cf_defined_partner, 'hx_pd': None},
                    'NPLL-O': {'call': self.cf_undefined_partner, 'hx_pd': 'non-exchangable'},
                    'NPLLRS': {'call': self.cf_undefined_partner, 'hx_pd': 'one'},
                    'NPLLRU': {'call': self.cf_undefined_partner, 'hx_pd': 'ukv'},
                    'NPTL-B': {'call': self.cf_defined_one_year_risk, 'hx_pd': None},
                    'NPTL-O': {'call': self.cf_undefined_one_year_risk, 'hx_pd': None},
                    'ay_avg': {'call': self.cf_ay_avg, 'hx_pd': None}
                    }

        out = merge_two_dicts({'insurance_id': insurance_id},
                              switcher[insurance_id]['call'](age_insured,
                                                             sex_insured,
                                                             pension_age,
                                                             hx_pd=switcher[insurance_id]['hx_pd'],
                                                             **kwargs))
        return out

    def pv(self, cf, intrest):
        """ Returns present value of cash flows.

        Parameters:
        -----------
        cf: dict {'insurance_id: str, 'payments': series, 'age': int, 'pension_age': int}
        intrest: int, float or series
        """
        cfs = cf['payments']
        insurance_id = cf['insurance_id']
        year = pd.Series(cfs.index)
        self.yield_curve = x_to_series(intrest, len(cfs))

        if insurance_id in ['OPLL', 'NPLL-B', 'ay_avg']:
            pv_factors = self.yield_curve.map(lambda r: 1. / (1 + r / 100.))**year
        elif insurance_id in ['NPTL-B', 'NPTL-O']:
            pv_factors = self.yield_curve.map(lambda r: 1. / (1 + r / 100.))**(year + 0.5)
        elif insurance_id in ['NPLL-O', 'NPLLRS', 'NPLLRU']:
            nyears_till_pension_age = cf['pension_age'] - cf['age']
            year = year + 0.5 * (year <= nyears_till_pension_age)
            pv_factors = self.yield_curve.map(lambda r: 1. / (1 + r / 100.))**year
        else:
            print("---ERROR: cannot process insurance_id: {0}".format(insurance_id))

        present_value = sum(cfs * pv_factors)
        rounding = self.params['round']
        return round(present_value, rounding)

    def run_test(self):
        """ Performs tariff calulations om testdata.

        Parameters:
        ----------
        d: dict.
        """
        msg1, msg2, msg3 = ("Generating cash flows...please wait...",
                            "Calculating present value of cash flows...",
                            "Sum of Errors Squared = ")
        print(msg1)
        testdata = self.testdata
        map_to_cashflows = lambda row: self.cf(insurance_id=row['insurance_id'],
                                               age_insured=row['age'],
                                               sex_insured=row['sex'],
                                               pension_age=row['pension_age'],
                                               intrest=row['intrest'])
        map_to_present_value = lambda row: self.pv(cf=row['cf'], intrest=row['intrest'])

        testdata['cf'] = testdata.apply(map_to_cashflows, axis=1)
        print(msg2)
        testdata['calculated'] = testdata.apply(map_to_present_value, axis=1)
        testdata['difference'] = testdata['test_value'] - testdata['calculated']
        del testdata['cf']
        error_squared = sum(testdata['difference'] * testdata['difference'])
        print(msg3),
        print(error_squared)
        return testdata

    def performance_test(self):
        """ Equal to run_test() but returns output to screen.

        Parameters:
        ----------
        d: dict.
        """

        testdata = self.testdata

        for row in testdata.itertuples():
            cfs = self.cf(row.insurance_id,
                          row.age,
                          row.sex,
                          row.pension_age,
                          intrest=row.intrest)
            calculated = self.pv(cfs, row.intrest)
            print("#{0} -- {1} -- {2}".format(row.Index, row.insurance_id, row.test_value - calculated))

    def calculate_cashflows(self, pension_age, intrest=3):
        """ Returns table with cashflows per insurance_id and age.

        Parameters:
        -----------
        pension_age: int
        intrest: int, float or Series. Default 3 pct.
        """

        # create table layout with all desired tariff combinations
        df = cartesian(lists=[INSURANCE_IDS, [MALE, FEMALE], range(LOWAGE, UPAGE)],
                       colnames=['insurance_id', 'sex_insured', 'age_insured'])

        # generate cashflows
        def map_to_cf(row):
            return self.cf(insurance_id=row['insurance_id'],
                           age_insured=row['age_insured'],
                           sex_insured=row['sex_insured'],
                           pension_age=pension_age,
                           intrest=intrest)
        df['cf'] = df.apply(map_to_cf, axis=1)
        self.intrest = intrest
        self.pension_age = pension_age
        self.cfs = df
        return df

    def calculate_factors(self, intrest, pension_age=67):
        """ Returns factors.

        Parameters:
        -----------
        intrest: int, float or Series.
        pension_age: int. Default 67 year.
        """
        if (intrest == self.intrest) and (pension_age == self.pension_age):
            factors = self.cfs
        else:
            self.cfs = self.calculate_cashflows(intrest=intrest, pension_age=pension_age)
            factors = self.cfs.copy(deep=True)
        factors['tar'] = factors.apply(lambda row: self.pv(row['cf'], intrest=intrest), axis=1)
        factors.set_index(['insurance_id', 'sex_insured', 'age_insured'], inplace=True)
        factors.drop('cf', inplace=True, axis=1)
        self.factors = factors
        return factors

    def export(self, xlswb, intrest, pension_age=67):
        """ Exports results to given xlswb.

        Parameters:
        -----------
        xlswb: str
        intrest: int, float or Series.
        pension_age: int. Default 67 year.
        """
        if (intrest == self.intrest) and (pension_age == self.pension_age):
            result = self.factors
        else:
            result = self.calculate_factors(intrest=intrest, pension_age=pension_age)

        sheets = OrderedDict()
        sheets['legend'] = self.legend
        sheets['factors'] = result.unstack(['sex_insured', 'insurance_id'])
        temp = self.cfs.copy(deep=True)
        temp['cf'] = temp['cf'].map(lambda x: x['payments'])
        cashflows = expand(temp, 'cf')
        cashflows.set_index(['sex_insured', 'insurance_id', 'age_insured', 'year'], inplace=True)
        sheets['cashflows'] = cashflows.unstack(['sex_insured', 'insurance_id'])
        sheets['yield_curve'] = pd.DataFrame(self.yield_curve, columns=['intrest'])
        sheets['lx'] = pd.concat([self.lx[MALE], self.lx[FEMALE]], axis=1)
        sheets['hx'] = pd.concat([self.hx[MALE], self.hx[FEMALE]], axis=1)
        adjustments = pd.read_excel(XLSWB, sheetname='tbl_adjustments')
        sheets['adjustments'] = adjustments[adjustments['id'] == self.params['adjustments']]

        # write everything to Excel
        writer = pd.ExcelWriter(xlswb)
        for sheetname, content in sheets.iteritems():
            content.to_excel(writer, sheetname)
        writer.save()

        msg = "Ready. See {0} for output".format(xlswb)
        print(msg)
