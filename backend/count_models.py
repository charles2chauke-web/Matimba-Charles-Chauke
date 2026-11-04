"""Experimental count strengths from reported corners/yellow cards only."""
import math
import numpy as np
from scipy.stats import poisson,nbinom
from backend import models,history


def analyse(rows,fixture,cutoff,options,minimum):
    output={};missing={}
    home,away=history.normalized(fixture['home']),history.normalized(fixture['away'])
    for metric,market,line in [('corners','corners',7.5),('yellow','yellow_cards',2.5)]:
        selected=[r for r in rows if all(r['metrics'].get(metric+'_'+side) is not None for side in ('home','away'))]
        try:
            models.check_coverage(selected,home,away,max(30,minimum),options['min_team_matches'])
            weights=models.recency_weights(selected,cutoff,options['half_life_days'])
            effective=float(weights.sum()**2/np.square(weights).sum())
            if effective<max(30,minimum):raise models.ModelUnavailable('Insufficient effective count-statistics sample')
            for team in (home,away):
                subset=[r for r in selected if team in (r['home_key'],r['away_key'])]
                w=models.recency_weights(subset,cutoff,options['half_life_days'])
                if float(w.sum()**2/np.square(w).sum())<options['min_team_matches']:
                    raise models.ModelUnavailable('Insufficient effective team count-statistics sample')
            fit=models.fit_strength(selected,cutoff,options['half_life_days'],options['ridge'],metric)
            h,a=fit.rates(home,away)
            if not 0<h+a<=60:raise models.ModelUnavailable('Count estimate is outside the numerical bound')
            observed=[];expected=[]
            for row in selected:
                x,y=fit.rates(row['home_key'],row['away_key'])
                expected.append(x+y);observed.append(row['metrics'][metric+'_home']+row['metrics'][metric+'_away'])
            dof=max(1,len(selected)-len(fit.coefficients))
            dispersion=max(1.,float(np.sum(weights/weights.mean()*np.square(np.asarray(observed)-expected)/np.maximum(expected,.01)))/dof)
            if not math.isfinite(dispersion) or dispersion>20:
                raise models.ModelUnavailable('Count dispersion is unstable; prediction withheld')
            mean=h+a
            plain=float(poisson.sf(line,mean))
            adjusted=float(nbinom.sf(line,mean/(dispersion-1),1/dispersion)) if dispersion>1+1e-8 else plain
            output[market]={'method':'Opponent/venue-adjusted count strengths; dispersion-aware negative-binomial tail with Poisson comparison',
                'expected_total':mean,'dispersion':dispersion,'diagnostics':{**fit.diagnostics(),'effective_sample':effective},
                'markets':[{'market':market,'selection':'over','line':str(line),'probability':adjusted,
                            'comparison_probability':plain,'push':0.,'settlement':'regulation_90'}],
                'warning':'Reported full-match FT counts only. Model agreement is not calibrated predictive uncertainty.'}
        except (models.ModelUnavailable,ValueError,FloatingPointError) as error:
            missing[market]=str(error)
    return output,missing
